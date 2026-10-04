from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.context import request_id_var
from app.core.ids import new_id
from app.models import AuditLog


class AuditService:
    """Writes inside the caller's transaction so the audit record and the action commit atomically."""

    def __init__(self, db: Session):
        self.db = db

    def record(self, *, action: str, resource_type: str, resource_id: str | None, actor: str = "api_key",
               client_id: str | None = None, api_key_id: str | None = None, ip: str | None = None,
               details: dict | None = None) -> None:
        self.db.add(AuditLog(id=new_id("audit"), client_id=client_id, api_key_id=api_key_id, actor=actor,
                             action=action, resource_type=resource_type, resource_id=resource_id,
                             request_id=request_id_var.get(), ip=ip, details=details or {}))
