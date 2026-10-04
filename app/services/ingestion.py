"""Document intake for single and bulk submissions (validation -> storage -> DB -> queue)."""
from __future__ import annotations

import hashlib
import json
import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.context import request_id_var
from app.core.errors import AppError, Conflict, PayloadTooLarge, RateLimited, ServiceUnavailable, ValidationFailed
from app.core.ids import new_id
from app.core.time import as_utc, utcnow
from app.models import Batch, IdempotencyKey, Process
from app.observability import tracing
from app.observability.metrics import DOCS_REJECTED, DOCS_SUBMITTED, QUOTA_REJECTED
from app.queue.base import JobQueue
from app.repositories.process import ProcessRepository
from app.repositories.tenancy import ClientRepository
from app.security import sandbox
from app.security.file_validation import ValidatedFile, validate_upload
from app.services.audit import AuditService
from app.services.principal import Principal
from app.storage.base import ObjectStorage, StorageError, document_key

log = logging.getLogger(__name__)


@dataclass
class IncomingFile:
    filename: str | None
    stream: BinaryIO


@dataclass
class Outcome:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


class _IdemRace(Exception):
    """A concurrent request with the same Idempotency-Key committed first."""


@dataclass
class _Item:
    index: int
    filename: str | None
    validated: ValidatedFile | None = None
    error: dict | None = None
    exc: AppError | None = None
    process_id: str | None = None
    storage_key: str | None = None


def _err(exc: AppError) -> dict:
    return {"code": exc.code, "message": exc.message}


def _seconds_until(dt: datetime) -> int:
    return max(1, int((dt - utcnow()).total_seconds()))


class IngestionService:
    def __init__(self, db: Session, storage: ObjectStorage, queue: JobQueue, settings: Settings):
        self.db, self.storage, self.queue, self.settings = db, storage, queue, settings
        self.procs = ProcessRepository(db)
        self.clients = ClientRepository(db)
        self.audit = AuditService(db)
        self._sandbox = sandbox.SandboxLimits(settings.render_timeout_seconds, settings.render_memory_mb)

    # ------------------------------------------------------------------ public API
    def submit_single(self, p: Principal, upload: IncomingFile, metadata: dict, idem_key: str | None) -> Outcome:
        return self._submit(p, [upload], metadata, idem_key, batch=False)

    def submit_bulk(self, p: Principal, uploads: list[IncomingFile], metadata: dict, idem_key: str | None) -> Outcome:
        if not uploads:
            raise ValidationFailed("No files provided", code="no_files")
        if len(uploads) > p.plan.max_batch_size:
            raise PayloadTooLarge(f"Batch exceeds the maximum of {p.plan.max_batch_size} files", code="batch_too_large")
        return self._submit(p, uploads, metadata, idem_key, batch=True)

    # ------------------------------------------------------------------ core flow
    def _submit(self, p: Principal, uploads: list[IncomingFile], metadata: dict, idem_key: str | None,
                *, batch: bool) -> Outcome:
        depth = self.queue.depth(bulk=batch)
        limit = self.settings.bulk_queue_max_depth if batch else self.settings.queue_max_depth
        if depth is not None and depth >= limit:
            raise ServiceUnavailable("Processing queue is saturated, retry later", code="queue_saturated",
                                     headers={"Retry-After": "30"})

        max_file = min(p.plan.max_file_bytes, self.settings.max_upload_bytes)
        items: list[_Item] = []
        total_bytes = 0
        for i, up in enumerate(uploads):
            item = _Item(i, None)
            try:
                v = validate_upload(up.stream, filename=up.filename, max_bytes=max_file,
                                    max_pdf_pages=min(self.settings.max_pdf_pages, p.plan.max_pages_per_document),
                                    max_image_pixels=self.settings.max_image_pixels,
                                    max_pdf_objects=self.settings.max_pdf_objects,
                                    max_decoded_bytes=self.settings.max_pdf_decoded_bytes,
                                    sandbox_limits=self._sandbox)
                total_bytes += v.size
                if batch and total_bytes > p.plan.max_batch_bytes:
                    raise PayloadTooLarge("Total batch size exceeds the plan limit", code="batch_too_large")
                item.validated, item.filename = v, v.safe_filename
            except PayloadTooLarge as exc:
                if exc.code == "batch_too_large":
                    raise
                item.error, item.exc = _err(exc), exc
            except AppError as exc:
                item.error, item.exc = _err(exc), exc
            items.append(item)
        for it in items:
            if it.error:
                DOCS_REJECTED.labels(reason=it.error["code"]).inc()

        valid = [it for it in items if it.validated]
        if not valid:
            if not batch:
                raise items[0].exc  # type: ignore[misc]  # preserve 413/415/422 semantics
            raise ValidationFailed("No valid files in batch", code="no_valid_files",
                                   details=[{"index": it.index, "error": it.error} for it in items])

        req_hash = self._request_hash(batch, items, metadata)
        if idem_key:
            replay = self._idempotent_replay(p, idem_key, req_hash)
            if replay:
                return replay

        # 1) Upload objects first (no DB txn held during network I/O).
        for it in valid:
            it.process_id = new_id("process")
            it.storage_key = document_key(p.client_id, it.process_id, it.validated.extension)  # type: ignore[union-attr]
        try:
            for it in valid:
                with tracing.span("storage.put"):
                    self.storage.put(it.storage_key, it.validated.data, it.validated.content_type)  # type: ignore[arg-type,union-attr]
        except StorageError as exc:
            self._cleanup(valid)
            raise ServiceUnavailable("Storage unavailable, retry later", code="storage_unavailable",
                                     headers={"Retry-After": "10"}) from exc

        # 2) One short transaction: lock tenant -> capacity -> rows -> idempotency record.
        try:
            outcome, accepted = self._persist(p, items, valid, metadata, idem_key, req_hash, batch)
        except (IntegrityError, _IdemRace):
            self.db.rollback()
            self._cleanup(valid)
            replay = self._idempotent_replay(p, idem_key, req_hash) if idem_key else None
            if replay:
                return replay
            raise
        except Exception:
            self.db.rollback()
            self._cleanup(valid)
            raise

        # 3) Enqueue after commit. Failure is tolerated: the reaper re-enqueues 'queued' rows.
        enqueued: list[str] = []
        for it in accepted:
            try:
                self.queue.enqueue(it.process_id, bulk=batch)  # type: ignore[arg-type]
                enqueued.append(it.process_id)  # type: ignore[arg-type]
            except Exception:
                log.exception("enqueue failed; reaper will retry", extra={"process_id": it.process_id})
        try:  # the submission is already durable; bookkeeping failure must not turn it into an error
            self.procs.mark_enqueued(enqueued)
            self.db.commit()
        except Exception:
            self.db.rollback()
            log.exception("mark_enqueued failed; reaper will re-enqueue")
        DOCS_SUBMITTED.labels(kind="batch" if batch else "single").inc(len(accepted))
        return outcome

    # ------------------------------------------------------------------ helpers
    def _persist(self, p: Principal, items: list[_Item], valid: list[_Item], metadata: dict,
                 idem_key: str | None, req_hash: str, batch: bool) -> tuple[Outcome, list[_Item]]:
        self.clients.lock(p.client_id)
        if idem_key:  # re-check under the lock: a concurrent twin may have committed
            existing = self._load_idem(p.client_id, idem_key)
            if existing is not None:
                raise _IdemRace()

        remaining, reason, retry_after = self._capacity(p)
        if remaining <= 0:
            QUOTA_REJECTED.labels(reason=reason).inc()
            raise RateLimited(f"Plan limit reached ({reason})", retry_after=retry_after, code=self._limit_code(reason))

        accepted, overflow = valid[:remaining], valid[remaining:]
        for it in overflow:
            QUOTA_REJECTED.labels(reason=reason).inc()
            it.error = {"code": self._limit_code(reason),
                        "message": "Plan capacity reached; resubmit this file later", "retryable": True}
        self._cleanup(overflow)

        now = utcnow()
        retention = p.client.retention_days or p.plan.retention_days or self.settings.default_retention_days
        batch_id = None
        if batch:
            batch_id = new_id("batch")
            self.procs.add(Batch(id=batch_id, client_id=p.client_id, total=len(items),
                                 rejected=len(items) - len(accepted)))
            self.db.flush()
        for it in accepted:
            v = it.validated
            self.procs.add(Process(
                id=it.process_id, client_id=p.client_id, batch_id=batch_id, status="queued",
                original_filename=v.safe_filename, content_type=v.content_type, file_size=v.size,  # type: ignore[union-attr]
                sha256=v.sha256, storage_key=it.storage_key, client_metadata=metadata,  # type: ignore[union-attr]
                max_attempts=self.settings.job_max_attempts, request_id=request_id_var.get(), expires_at=now + timedelta(days=retention)))
        self.db.flush()

        body = self._body(batch, batch_id, items, accepted, now)
        if idem_key:
            self.db.add(IdempotencyKey(id=new_id("idem"), client_id=p.client_id, key=idem_key, request_hash=req_hash,
                                       status_code=202, response_body=body, created_at=now,
                                       expires_at=now + timedelta(hours=self.settings.idempotency_ttl_hours)))
        self.audit.record(action="bills.submit_batch" if batch else "bills.submit", resource_type="batch" if batch else "process",
                          resource_id=batch_id or accepted[0].process_id, client_id=p.client_id,
                          api_key_id=p.api_key_id, ip=p.ip, details={"accepted": len(accepted), "total": len(items)})
        self.db.commit()
        return Outcome(202, body), accepted

    def _body(self, batch: bool, batch_id: str | None, items: list[_Item], accepted: list[_Item], now: datetime) -> dict:
        acc = {it.index for it in accepted}
        if not batch:
            it = accepted[0]
            return {"process_id": it.process_id, "status": "queued", "created_at": now.isoformat()}
        procs = []
        for it in items:
            if it.index in acc:
                procs.append({"index": it.index, "filename": it.filename, "process_id": it.process_id, "status": "queued"})
            else:
                procs.append({"index": it.index, "filename": it.filename, "process_id": None, "status": "rejected",
                              "error": it.error})
        return {"batch_id": batch_id, "status": "queued", "accepted": len(accepted),
                "rejected": len(items) - len(accepted), "created_at": now.isoformat(), "processes": procs}

    @staticmethod
    def _limit_code(reason: str) -> str:
        return {"concurrency": "concurrency_limit_exceeded", "rate": "document_rate_exceeded"}.get(reason, "quota_exceeded")

    def _capacity(self, p: Principal) -> tuple[int, str, int]:
        now = utcnow()
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        nxt_day = day0 + timedelta(days=1)
        month0 = day0.replace(day=1)
        nxt_month = month0.replace(day=1) + timedelta(days=monthrange(month0.year, month0.month)[1])
        cands = [
            (p.plan.max_concurrent_jobs - self.procs.count_active(p.client_id), "concurrency", 30),
            (p.plan.daily_quota - self.procs.count_created_since(p.client_id, day0), "daily", _seconds_until(nxt_day)),
            (p.plan.monthly_quota - self.procs.count_created_since(p.client_id, month0), "monthly", _seconds_until(nxt_month)),
        ]
        minute_ago = now - timedelta(seconds=60)
        used = self.procs.count_created_since(p.client_id, minute_ago)
        rate_left = p.plan.documents_per_minute - used
        best = min(cands, key=lambda c: c[0])
        if rate_left < best[0]:
            oldest = self.procs.oldest_created_since(p.client_id, minute_ago)
            wait = 60 - int((now - as_utc(oldest)).total_seconds()) + 1 if oldest else 60
            return rate_left, "rate", max(1, wait)
        return best

    @staticmethod
    def _request_hash(batch: bool, items: list[_Item], metadata: dict) -> str:
        parts = [it.validated.sha256 if it.validated else f"invalid:{it.index}" for it in items]
        blob = json.dumps({"batch": batch, "files": parts, "meta": metadata}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _load_idem(self, client_id: str, key: str) -> IdempotencyKey | None:
        row = self.db.query(IdempotencyKey).filter_by(client_id=client_id, key=key).one_or_none()
        if row and as_utc(row.expires_at) <= utcnow():
            self.db.delete(row)
            self.db.flush()
            return None
        return row

    def _idempotent_replay(self, p: Principal, key: str, req_hash: str) -> Outcome | None:
        row = self._load_idem(p.client_id, key)
        if row is None:
            return None
        if row.request_hash != req_hash:
            raise ValidationFailed("Idempotency-Key was already used with a different request",
                                   code="idempotency_key_mismatch")
        return Outcome(row.status_code, row.response_body, replayed=True)

    def _cleanup(self, items: list[_Item]) -> None:
        for it in items:
            if it.storage_key:
                try:
                    self.storage.delete(it.storage_key)
                except Exception:
                    log.warning("orphan object cleanup failed", extra={"key": it.storage_key})
