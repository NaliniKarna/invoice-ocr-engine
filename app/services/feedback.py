from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.ids import new_id
from app.feedback.validation import validate_corrections
from app.models import Feedback
from app.repositories.process import ProcessRepository
from app.schemas.bills import FeedbackRequest, FeedbackResponse
from app.services import state
from app.services.audit import AuditService
from app.services.principal import Principal


class FeedbackService:
    """Stores validated corrections. NEVER mutates the original result and NEVER touches models."""

    def __init__(self, db: Session, settings: Settings):
        self.db, self.settings = db, settings
        self.procs = ProcessRepository(db)
        self.audit = AuditService(db)

    def submit(self, p: Principal, process_id: str, req: FeedbackRequest) -> FeedbackResponse:
        proc = self.procs.get(p.client_id, process_id)  # tenant-scoped: other tenants' ids -> 404
        if proc is None:
            raise NotFound("Process not found", code="process_not_found")
        if proc.status != state.COMPLETED:
            raise Conflict("Feedback can only be submitted for completed processes", code="invalid_state")
        res = self.procs.get_result(p.client_id, process_id)
        if res is None or res.data is None:
            raise Conflict("Result is no longer available", code="result_unavailable")

        valid, errors = validate_corrections([(c.field, c.value) for c in req.corrections], res.data)
        if errors:
            raise ValidationFailed("One or more corrections are invalid; nothing was stored",
                                   code="invalid_corrections", details=errors)
        sub = new_id("submission")
        ids: list[str] = []
        for v in valid:
            fid = new_id("feedback")
            ids.append(fid)
            self.db.add(Feedback(
                id=fid, submission_id=sub, client_id=p.client_id, process_id=process_id, field_path=v.field,
                original_value=v.original, corrected_value=v.corrected,
                ocr_model_version_id=res.ocr_model_version_id, extractor_model_version_id=res.extractor_model_version_id,
                api_key_id=p.api_key_id, comment=req.comment, status="validated"))
        self.audit.record(action="feedback.submit", resource_type="process", resource_id=process_id,
                          client_id=p.client_id, api_key_id=p.api_key_id, ip=p.ip,
                          details={"submission_id": sub, "fields": [v.field for v in valid]})
        self.db.commit()
        return FeedbackResponse(submission_id=sub, accepted=len(valid), feedback_ids=ids)
