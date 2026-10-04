"""The OCR job as a plain function (Celery-free => unit-testable, engine-agnostic)."""
from __future__ import annotations

import json
import logging
import random
import time
from types import SimpleNamespace
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.context import process_id_var, request_id_var, tenant_id_var
from app.core.ids import new_id
from app.core.time import as_utc, utcnow
from app.extraction.base import Extractor
from app.extraction.pipeline import DocumentPipeline
from app.extraction.normalization import NORMALIZATION_VERSION
from app.extraction.schema import SCHEMA_VERSION
from app.extraction.validation import VALIDATION_VERSION
from app.layout.base import LAYOUT_VERSION
PIPELINE_VERSION = "2"
from app.models import Client, OcrResult, Process
from app.observability import metrics as m
from app.observability import tracing
from app.ocr.base import OCRError, OCRProvider
from app.ml.runtime import ModelRuntime
from app.ocr.preprocess import PREPROCESS_VERSION, load_pages_sandboxed
from app.ocr.preprocess_ops import preprocess_version
from app.repositories.model_versions import ModelVersionRepository
from app.repositories.process import ProcessRepository
from app.repositories.tenancy import ClientRepository
from app.security.sandbox import SandboxLimits
from app.services import state
from app.storage.base import ObjectStorage, StorageError, ocr_artifact_key

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"      # not claimable (duplicate delivery / cancelled / deleted / superseded attempt)
    DEFERRED = "deferred"    # tenant at its running-jobs cap: job stays `queued`, redelivered later
    RETRY = "retry"
    FAILED = "failed"


@dataclass
class JobResult:
    outcome: Outcome
    countdown: int = 0


@dataclass
class WorkerContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    storage: ObjectStorage
    ocr: OCRProvider
    extractor: Extractor           # the REFERENCE extractor: always available, used whenever no registry model is usable
    runtime: "ModelRuntime | None" = None
    ocr_retry: OCRProvider | None = None   # provider configuration for the second pass (see Settings.ocr_retry_*)

    def __post_init__(self) -> None:
        if self.runtime is None:
            self.runtime = ModelRuntime(self.settings, self.session_factory, self.storage, self.extractor)

    @property
    def pipeline(self) -> DocumentPipeline:
        from decimal import Decimal
        from app.extraction.rules import RuleBasedExtractor
        from app.extraction.validation import ValidationConfig
        fb = self.extractor if isinstance(self.extractor, RuleBasedExtractor) else RuleBasedExtractor()
        cfg = ValidationConfig(tolerance_abs=Decimal(str(self.settings.validation_tolerance_abs)),
                               tolerance_rel=Decimal(str(self.settings.validation_tolerance_rel)),
                               required_fields=self.settings.validate_required_fields)
        from app.extraction.pipeline import RetryPlan
        from app.ocr.providers import build_retry_provider_for, retry_variants
        plans: list[RetryPlan] = []
        if self.ocr_retry is not None:
            for n, (ops, psm) in enumerate(retry_variants(self.settings)):
                # the first variant uses the provider injected into the context (tests, custom engines); the others are built per psm
                prov = self.ocr_retry if n == 0 else build_retry_provider_for(self.settings, psm)
                if prov is None:
                    continue
                plans.append(RetryPlan(prov, ops, below_confidence=self.settings.retry_below_confidence, max_pages=self.settings.retry_max_pages,
                                       psm=psm if self.settings.ocr_provider == "tesseract" else None, on_key_fields=self.settings.retry_on_key_fields))
        return DocumentPipeline(self.ocr, self.runtime.current(), day_first=self.settings.assume_day_first_dates,
                                review_threshold=self.settings.review_confidence_threshold, fallback=fb, validation=cfg,
                                fail_on_partial=self.settings.partial_page_policy == "fail", retry=plans or None,
                                alternates=self._alternates())

    def _alternates(self) -> list[Extractor]:
        from app.core.container import extractor_by_name
        names = [n.strip() for n in self.settings.extractor_alternates.split(",") if n.strip()]
        return [extractor_by_name(n) for n in names]

    @property
    def preprocess_ops(self) -> list[str]:
        from app.ocr.preprocess_ops import parse_ops
        return parse_ops(self.settings.preprocess_ops)

    @property
    def sandbox_limits(self) -> SandboxLimits:
        return SandboxLimits(self.settings.render_timeout_seconds, self.settings.render_memory_mb)


_SAFE_MESSAGES = {
    "ocr_not_configured": "No OCR engine is configured for this deployment",
    "ocr_failed": "The document could not be read",
    "extraction_failed": "The document could not be interpreted",
    "document_too_complex": "The document is too complex to process safely",
    "storage_error": "Temporary storage failure",
    "internal_error": "Internal processing error",
    "worker_timeout": "Processing timed out",
}


def run_job(process_id: str, ctx: WorkerContext, carrier: dict[str, str] | None = None) -> JobResult:
    db = ctx.session_factory()
    process_id_var.set(process_id)
    m.JOBS_RECEIVED.inc()
    try:
        with tracing.continued(carrier, "job.run"):
            return _run(db, process_id, ctx)
    finally:
        db.close()
        for var in (process_id_var, tenant_id_var, request_id_var):
            var.set(None)


def _safe_rollback(db: Session) -> None:
    """Roll back; if the connection is already dead, drop it so the session can start a fresh one."""
    try:
        db.rollback()
    except Exception:
        log.warning("rollback failed; discarding connection")
        try:
            db.close()
        except Exception:
            pass


def _skip(db: Session, why: str) -> JobResult:
    db.rollback()
    m.JOB_OUTCOMES.labels(outcome="skipped").inc()
    log.info("job skipped", extra={"reason": why})
    return JobResult(Outcome.SKIPPED)


def _claim(db: Session, procs: ProcessRepository, process_id: str, ctx: WorkerContext) -> tuple[Process, int] | JobResult:
    """Fair-share claim: serialise per tenant on the client row, enforce the tenant's running-jobs cap, then CAS."""
    head = db.get(Process, process_id)
    if head is None or head.deleted_at is not None or head.status != state.QUEUED:
        return _skip(db, "not_queued")
    client = db.get(Client, head.client_id)
    ClientRepository(db).lock(head.client_id)
    alive_since = utcnow() - timedelta(seconds=ctx.settings.stale_processing_seconds)
    if procs.count_running(head.client_id, started_after=alive_since) >= client.plan.max_running_jobs:
        db.rollback()
        m.JOBS_DEFERRED.inc()
        return JobResult(Outcome.DEFERRED, countdown=ctx.settings.worker_defer_seconds + random.randint(0, 5))
    attempt = procs.claim(process_id)
    if attempt is None:
        return _skip(db, "lost_claim")
    db.commit()
    db.refresh(head)
    return head, attempt


def _run(db: Session, process_id: str, ctx: WorkerContext) -> JobResult:
    procs = ProcessRepository(db)
    claimed = _claim(db, procs, process_id, ctx)
    if isinstance(claimed, JobResult):
        return claimed
    head, attempt = claimed
    # Snapshot the fields the long phase needs, then END the transaction. Otherwise the session sits
    # "idle in transaction" through OCR and Postgres kills the connection (idle_in_transaction_session_timeout).
    proc = SimpleNamespace(id=head.id, client_id=head.client_id, request_id=head.request_id,
                           enqueued_at=head.enqueued_at, content_type=head.content_type,
                           file_size=head.file_size, storage_key=head.storage_key)
    db.commit()
    tenant_id_var.set(proc.client_id)
    request_id_var.set(proc.request_id)   # correlation: HTTP request -> this job
    t_start = time.perf_counter()
    qlat = None
    if proc.enqueued_at:
        qlat = max(0.0, (utcnow() - as_utc(proc.enqueued_at)).total_seconds())
        m.QUEUE_LATENCY.observe(qlat)
    log.info("job started", extra={"attempt": attempt, "queue_latency_ms": None if qlat is None else int(qlat * 1000),
                                    "content_type": proc.content_type, "size": proc.file_size})
    m.WORKER_ACTIVE_JOBS.inc()
    try:
        with tracing.span("storage.get"):
            raw = ctx.storage.get(proc.storage_key)
        t_r = time.perf_counter()
        with tracing.span("job.render"):
            pages = load_pages_sandboxed(raw, proc.content_type, max_pages=ctx.settings.max_pdf_pages,
                                         max_pixels=ctx.settings.max_image_pixels, limits=ctx.sandbox_limits, ops=ctx.preprocess_ops)
        render_ms = int((time.perf_counter() - t_r) * 1000)
        out = ctx.pipeline.run(pages)
        key = ocr_artifact_key(proc.client_id, proc.id)
        with tracing.span("storage.put"):
            ctx.storage.put(key, json.dumps(out.ocr.to_dict()).encode(), "application/json")

        models = ModelVersionRepository(db)
        used = out.extractor   # the extractor that really produced the result (may be the fallback)
        retry_used = bool(out.retry and out.retry.get("used"))
        # the recorded preprocessing is the one that produced the stored result (the retry ops are applied on top of the base ops)
        pp_version = preprocess_version(PREPROCESS_VERSION, ctx.preprocess_ops + (out.retry["ops"] if retry_used else []))
        ocr_mv = models.ensure("ocr", ctx.ocr.name, ctx.ocr.version)
        ext_mv = models.ensure("extractor", used.name, used.version)
        total_ms = int((time.perf_counter() - t_start) * 1000)
        ex = out.extraction
        meta = {"versions": {"normalization": NORMALIZATION_VERSION, "validation": VALIDATION_VERSION, "layout": LAYOUT_VERSION,
                             "confidence_method": ex.confidence_method, "pipeline": PIPELINE_VERSION},
                "calibrated": ex.calibrated, "fallback_from": out.fallback_from, "failed_pages": out.failed_pages,
                "pages": [{"page": pg.number, "status": pg.status, "error_code": pg.error_code, "rotation": pg.rotation}
                          for pg in out.ocr.pages],
                "evidence": {k: v.model_dump(mode="json") for k, v in ex.evidence.items()},
                "item_evidence": ex.item_evidence}
        if out.strategies:
            meta["extraction_strategies"] = out.strategies
        if out.retry:
            meta["ocr_retry"] = out.retry
        with tracing.span("db.persist_result"):
            db.add(OcrResult(
                id=new_id("result"), process_id=proc.id, client_id=proc.client_id, schema_version=SCHEMA_VERSION,
                preprocessing_version=pp_version,
                data=ex.data.model_dump(mode="json"), field_confidence=ex.field_confidence,
                issues=[i.model_dump() for i in ex.issues], confidence=ex.confidence,
                requires_review=ex.requires_review, ocr_artifact_key=key, ocr_model_version_id=ocr_mv.id,
                extractor_model_version_id=ext_mv.id, page_count=len(pages), meta=meta, ocr_ms=out.ocr_ms,
                extraction_ms=out.extraction_ms, total_ms=total_ms))
            db.flush()
            # Fenced CAS: only the attempt that still owns the job may complete it (a stale slow worker loses).
            if not procs.transition(proc.id, state.COMPLETED, sources=[state.PROCESSING], attempt=attempt,
                                    completed_at=utcnow(), ocr_model_version_id=ocr_mv.id,
                                    extractor_model_version_id=ext_mv.id, error_code=None, error_message=None,
                                    error_retryable=None):
                return _skip(db, "superseded_attempt")
            db.commit()
        m.JOB_OUTCOMES.labels(outcome="completed").inc()
        m.JOB_DURATION.observe(total_ms / 1000)
        m.OCR_DURATION.labels(provider=ctx.ocr.name, model_version=ctx.ocr.version).observe(out.ocr_ms / 1000)
        m.EXTRACT_DURATION.labels(extractor=used.name, model_version=used.version).observe(out.extraction_ms / 1000)
        m.CONFIDENCE.observe(ex.confidence)
        m.OCR_PAGES.observe(len(out.ocr.pages))
        for pg in out.ocr.pages:
            if pg.status != "ok":
                m.OCR_PAGE_FAILURES.labels(provider=ctx.ocr.name, code=(pg.error_code or "unknown")[:40]).inc()
        for iss in ex.issues:
            m.VALIDATION_ISSUES.labels(code=iss.code[:40], severity=iss.severity).inc()
        if ex.confidence < ctx.settings.review_confidence_threshold:
            m.LOW_CONFIDENCE.inc()
        if ex.requires_review:
            m.REVIEW_REQUIRED.inc()
        log.info("job completed", extra={
            "total_ms": total_ms, "render_ms": render_ms, "ocr_ms": out.ocr_ms, "extraction_ms": out.extraction_ms,
            "pages": len(pages), "confidence": ex.confidence, "requires_review": ex.requires_review,
            "ocr_model": f"{ctx.ocr.name}@{ctx.ocr.version}",
            "extractor_model": f"{used.name}@{used.version}",
            "preprocessing_version": pp_version, "schema_version": SCHEMA_VERSION})
        return JobResult(Outcome.COMPLETED)
    except Exception as exc:  # classify, never leak
        _safe_rollback(db)
        return _handle_failure(db, procs, proc.id, attempt, exc)
    finally:
        m.WORKER_ACTIVE_JOBS.dec()


def _handle_failure(db: Session, procs: ProcessRepository, process_id: str, attempt: int, exc: Exception) -> JobResult:
    proc = db.get(Process, process_id)
    db.refresh(proc)
    if isinstance(exc, OCRError):
        code, retryable, kind = exc.code, exc.retryable, "ocr"
    elif isinstance(exc, StorageError):
        code, retryable, kind = "storage_error", True, "storage"
    else:
        code, retryable, kind = "internal_error", True, "unexpected"
        log.exception("unexpected job failure")
    m.WORKER_ERRORS.labels(kind=kind).inc()
    if retryable and attempt < proc.max_attempts:
        if not procs.transition(process_id, state.QUEUED, sources=[state.PROCESSING], attempt=attempt, error_code=code):
            return _skip(db, "superseded_attempt")
        db.commit()
        m.JOB_RETRIES.inc()
        m.JOB_OUTCOMES.labels(outcome="retry").inc()
        log.warning("job will retry", extra={"error_code": code, "attempt": attempt})
        return JobResult(Outcome.RETRY, countdown=min(300, 10 * 2 ** attempt))   # 20s, 40s, 80s ... capped 5min
    if not procs.transition(process_id, state.FAILED, sources=[state.PROCESSING], attempt=attempt,
                            completed_at=utcnow(), error_code=code,
                            error_message=_SAFE_MESSAGES.get(code, _SAFE_MESSAGES["internal_error"]),
                            error_retryable=False):
        return _skip(db, "superseded_attempt")
    db.commit()
    m.JOB_OUTCOMES.labels(outcome="failed").inc()
    log.error("job failed permanently", extra={"error_code": code, "attempts": attempt})
    return JobResult(Outcome.FAILED)
