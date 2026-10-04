"""Read/cancel/delete use-cases. All lookups go through tenant-scoped repository methods;
a process belonging to another tenant is indistinguishable from a non-existent one (404)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.core.time import utcnow
from app.extraction.schema import InvoiceData, ValidationIssue
from app.models import Process
from app.repositories.model_versions import ModelVersionRepository
from app.repositories.process import ProcessRepository
from app.schemas.bills import ApiError, BatchDetail, ModelVersions, PipelineVersions, ProcessDetail, ResultPayload
from app.services import state
from app.services.audit import AuditService
from app.services.principal import Principal
from app.storage.base import ObjectStorage, StorageError


class ProcessService:
    def __init__(self, db: Session, storage: ObjectStorage):
        self.db, self.storage = db, storage
        self.procs = ProcessRepository(db)
        self.models = ModelVersionRepository(db)
        self.audit = AuditService(db)

    def _require(self, p: Principal, process_id: str) -> Process:
        proc = self.procs.get(p.client_id, process_id)
        if proc is None:
            raise NotFound("Process not found", code="process_not_found")
        return proc

    def get_detail(self, p: Principal, process_id: str) -> ProcessDetail:
        return self._detail(p, self._require(p, process_id))

    def _detail(self, p: Principal, proc: Process) -> ProcessDetail:
        d = ProcessDetail(process_id=proc.id, status=proc.status, batch_id=proc.batch_id, created_at=proc.created_at,
                          updated_at=proc.updated_at, completed_at=proc.completed_at, attempts=proc.attempts,
                          metadata=proc.client_metadata or {})
        if proc.status == state.COMPLETED:
            res = self.procs.get_result(p.client_id, proc.id)
            if res is not None and res.data is not None:
                ocr_mv = self.models.get(res.ocr_model_version_id)
                ext_mv = self.models.get(res.extractor_model_version_id)
                d.result = ResultPayload.model_validate(res.data)
                d.confidence, d.field_confidence = res.confidence, res.field_confidence
                d.confidence_calibrated = bool((res.meta or {}).get("calibrated", False))
                d.pipeline_versions = PipelineVersions(preprocessing=res.preprocessing_version, schema_version=res.schema_version,
                                                 **{k: v for k, v in ((res.meta or {}).get("versions") or {}).items()
                                                    if k in PipelineVersions.model_fields})
                d.requires_review = res.requires_review
                d.issues = [ValidationIssue.model_validate(i) for i in res.issues]
                d.model_versions = ModelVersions(
                    ocr=f"{ocr_mv.name}@{ocr_mv.version}" if ocr_mv else None,
                    extractor=f"{ext_mv.name}@{ext_mv.version}" if ext_mv else None)
        elif proc.status == state.FAILED:
            d.error = ApiError(code=proc.error_code or "processing_failed",
                               message=proc.error_message or "Processing failed", retryable=proc.error_retryable)
        return d

    def get_batch(self, p: Principal, batch_id: str) -> BatchDetail:
        batch = self.procs.get_batch(p.client_id, batch_id)
        if batch is None:
            raise NotFound("Batch not found", code="batch_not_found")
        procs = self.procs.list_batch(p.client_id, batch_id)
        counts = {s: 0 for s in state.ALL}
        for pr in procs:
            counts[pr.status] += 1
        if any(counts[s] for s in state.ACTIVE):
            status = "processing" if counts[state.PROCESSING] or counts[state.COMPLETED] or counts[state.FAILED] else "queued"
        else:
            status = "completed" if not counts[state.FAILED] else ("failed" if not counts[state.COMPLETED] else "completed_with_errors")
        return BatchDetail(batch_id=batch.id, status=status, total=batch.total, rejected_at_intake=batch.rejected,
                           counts=counts, created_at=batch.created_at,
                           processes=[{"process_id": x.id, "status": x.status, "filename": x.original_filename} for x in procs])

    def cancel(self, p: Principal, process_id: str) -> ProcessDetail:
        proc = self._require(p, process_id)
        if not self.procs.transition(proc.id, state.CANCELLED, completed_at=utcnow()):
            self.db.rollback()
            raise Conflict(f"Cannot cancel a process in status '{proc.status}'", code="invalid_state")
        self.audit.record(action="bills.cancel", resource_type="process", resource_id=proc.id,
                          client_id=p.client_id, api_key_id=p.api_key_id, ip=p.ip)
        self.db.commit()
        self.db.refresh(proc)
        return self._detail(p, proc)

    def delete(self, p: Principal, process_id: str) -> None:
        proc = self._require(p, process_id)
        if proc.status in state.ACTIVE and proc.status == state.PROCESSING:
            raise Conflict("Cannot delete a document while it is being processed", code="invalid_state")
        try:
            purge_process(self.db, self.storage, proc, hard_feedback=not p.client.training_opt_in)
        except StorageError:
            self.db.rollback()   # nothing half-deleted in the DB; object deletion is idempotent so the client can retry
            raise
        self.audit.record(action="bills.delete", resource_type="process", resource_id=proc.id,
                          client_id=p.client_id, api_key_id=p.api_key_id, ip=p.ip)
        self.db.commit()


def purge_process(db: Session, storage: ObjectStorage, proc: Process, *, hard_feedback: bool) -> None:
    """Retention/erasure: remove document + raw OCR artifacts, tombstone extracted data, soft-delete row.
    Feedback is deleted unless the tenant opted in to model improvement (then it is retained as training data)."""
    from app.models import Feedback, OcrResult
    from sqlalchemy import delete as sa_delete

    keys = [proc.storage_key]
    res = db.query(OcrResult).filter_by(process_id=proc.id, client_id=proc.client_id).one_or_none()
    if res is not None:
        keys.append(res.ocr_artifact_key)
        res.data, res.ocr_artifact_key, res.field_confidence, res.issues = None, None, {}, []
        res.meta = {"versions": (res.meta or {}).get("versions", {})}   # evidence holds OCR source text: erased with the result
    for k in filter(None, keys):
        storage.delete(k)  # raises StorageError -> caller's transaction aborts, nothing half-deleted in DB
    if hard_feedback:
        db.execute(sa_delete(Feedback).where(Feedback.process_id == proc.id, Feedback.client_id == proc.client_id))
        # Same consent rule as feedback: without training consent nothing derived from the document is kept. Datasets that
        # already contain the example are flagged (see ExampleService.erase) and must be rebuilt.
        from app.ml.examples import ExampleService
        ExampleService(db, None).erase(proc.client_id, process_id=proc.id, reason="process_deleted")
    proc.storage_key, proc.client_metadata, proc.original_filename = None, {}, None
    proc.deleted_at = utcnow()
