"""Thin routes: parse -> call service -> shape response. No business logic here."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import (batch_id_param, bulk_rate_limit, get_container, get_db, idempotency_key, process_id_param, require_scope,
                          upload_rate_limit)
from app.core.container import Container
from app.schemas.bills import (BatchDetail, BulkAccepted, FeedbackRequest, FeedbackResponse, ProcessDetail,
                               SubmitAccepted)
from app.schemas.common import ErrorResponse
from app.schemas.common import parse_metadata
from app.services.feedback import FeedbackService
from app.services.ingestion import IncomingFile, IngestionService, Outcome
from app.services.principal import Principal
from app.services.processes import ProcessService

def _errs(*codes: int) -> dict:
    desc = {400: "Bad request", 401: "Missing, invalid, revoked or expired API key", 403: "API key lacks the required scope, or account suspended",
            404: "Not found (also returned for resources owned by another tenant)", 409: "Operation not allowed in the current state",
            413: "Payload too large", 415: "Unsupported file type",
            422: "Validation failed (details in error.details where applicable) or Idempotency-Key reused with a different payload",
            429: "Rate limit, concurrency limit or quota exceeded. Honour the Retry-After header.",
            503: "Dependency unavailable or queue saturated. Honour Retry-After; the request had no effect."}
    return {c: {"model": ErrorResponse, "description": desc[c]} for c in codes}


_COMMON = _errs(401, 403, 429, 503)
router = APIRouter(prefix="/api/v1/bills", tags=["bills"])


def _reply(o: Outcome, location: str | None) -> JSONResponse:
    headers = {}
    if location:
        headers["Location"] = location
    if o.replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(o.body, status_code=o.status_code, headers=headers)


@router.post("", status_code=202, summary="Submit one document for asynchronous processing",
             description="Multipart form: `file` (PNG/JPEG/PDF) and optional `metadata` (flat JSON object, stored opaque). "
                         "Send `Idempotency-Key` to make network retries safe: the same key+payload replays the original "
                         "202 (header `Idempotent-Replay: true`); the same key with a different payload returns 422.",
             response_model=SubmitAccepted, responses={**_COMMON, **_errs(413, 415, 422)})
def submit_bill(file: UploadFile = File(...), metadata: str | None = Form(default=None),
                idem: str | None = Depends(idempotency_key), p: Principal = Depends(upload_rate_limit),
                db: Session = Depends(get_db), c: Container = Depends(get_container)):
    svc = IngestionService(db, c.storage, c.queue, c.settings)
    meta = parse_metadata(metadata, c.settings.max_metadata_bytes)
    o = svc.submit_single(p, IncomingFile(file.filename, file.file), meta, idem)
    return _reply(o, f"/api/v1/bills/{o.body['process_id']}")


@router.post("/bulk", status_code=202, summary="Submit many documents in one request",
             description="Multipart form: repeated `files` field. Files are validated independently; invalid files come back as "
                         "`rejected` items while valid ones are queued. If plan capacity is partly exhausted a prefix is accepted "
                         "and the remainder is rejected with `retryable: true`. Bulk work uses a separate queue.",
             response_model=BulkAccepted, responses={**_COMMON, **_errs(413, 422)})
def submit_bulk(files: list[UploadFile] = File(...), metadata: str | None = Form(default=None),
                idem: str | None = Depends(idempotency_key), p: Principal = Depends(bulk_rate_limit),
                db: Session = Depends(get_db), c: Container = Depends(get_container)):
    svc = IngestionService(db, c.storage, c.queue, c.settings)
    meta = parse_metadata(metadata, c.settings.max_metadata_bytes)
    o = svc.submit_bulk(p, [IncomingFile(f.filename, f.file) for f in files], meta, idem)
    return _reply(o, f"/api/v1/bills/batches/{o.body['batch_id']}")


@router.get("/batches/{batch_id}", response_model=BatchDetail, summary="Batch status",
            responses={**_COMMON, **_errs(404)})
def get_batch(batch_id: str = Depends(batch_id_param), p: Principal = Depends(require_scope("bills:read")),
              db: Session = Depends(get_db), c: Container = Depends(get_container)):
    return ProcessService(db, c.storage).get_batch(p, batch_id)


@router.get("/{process_id}", response_model=ProcessDetail, response_model_exclude_none=True,
            summary="Process status and result", description="`confidence` is a provisional heuristic, not a calibrated probability.",
            responses={**_COMMON, **_errs(404)})
def get_bill(process_id: str = Depends(process_id_param), p: Principal = Depends(require_scope("bills:read")),
             db: Session = Depends(get_db), c: Container = Depends(get_container)):
    return ProcessService(db, c.storage).get_detail(p, process_id)


@router.post("/{process_id}/cancel", response_model=ProcessDetail, response_model_exclude_none=True,
             summary="Cancel a queued process", responses={**_COMMON, **_errs(404, 409)})
def cancel_bill(process_id: str = Depends(process_id_param), p: Principal = Depends(require_scope("bills:write")),
                db: Session = Depends(get_db), c: Container = Depends(get_container)):
    return ProcessService(db, c.storage).cancel(p, process_id)


@router.delete("/{process_id}", status_code=204, summary="Delete document and result",
               description="Synchronous: 204 means the stored document was removed. If storage is unavailable the call fails with 503 and can be retried.",
               responses={**_COMMON, **_errs(404, 409)})
def delete_bill(process_id: str = Depends(process_id_param), p: Principal = Depends(require_scope("bills:write")),
                db: Session = Depends(get_db), c: Container = Depends(get_container)):
    ProcessService(db, c.storage).delete(p, process_id)
    return Response(status_code=204)


@router.post("/{process_id}/feedback", status_code=201, response_model=FeedbackResponse,
             summary="Submit field corrections", description="Atomic: any invalid correction rejects the whole submission. The original result is never modified; corrections never change a deployed model.",
             responses={**_COMMON, **_errs(404, 409, 422)})
def submit_feedback(req: FeedbackRequest, process_id: str = Depends(process_id_param),
                    p: Principal = Depends(require_scope("feedback:write")), db: Session = Depends(get_db),
                    c: Container = Depends(get_container)):
    return FeedbackService(db, c.settings).submit(p, process_id, req)
