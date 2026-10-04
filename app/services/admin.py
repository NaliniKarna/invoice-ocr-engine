"""Tenant + API-key lifecycle (used by the internal admin API and ops scripts)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import BadRequest, NotFound
from app.core.ids import new_id
from app.core.time import utcnow
from app.models import ApiKey, Client
from app.repositories.tenancy import ApiKeyRepository, ClientRepository
from app.security.api_keys import DEFAULT_SCOPES, VALID_SCOPES, generate_api_key
from app.services.audit import AuditService


class AdminService:
    def __init__(self, db: Session, settings: Settings):
        self.db, self.settings = db, settings
        self.clients, self.keys = ClientRepository(db), ApiKeyRepository(db)
        self.audit = AuditService(db)

    def create_client(self, *, name: str, plan_id: str, training_opt_in: bool = False, training_policy: str | None = None) -> Client:
        if self.clients.get_plan(plan_id) is None:
            raise BadRequest("Unknown plan", code="unknown_plan")
        policy = training_policy or ("pooled" if training_opt_in else "none")
        if policy not in ("none", "tenant_only", "pooled"):
            raise BadRequest("Unknown training policy", code="invalid_policy")
        c = Client(id=new_id("client"), name=name, plan_id=plan_id, training_opt_in=policy == "pooled", training_policy=policy)
        self.db.add(c)
        self.audit.record(action="client.create", resource_type="client", resource_id=c.id, actor="admin", client_id=c.id)
        self.db.commit()
        return c

    def set_status(self, client_id: str, status: str) -> Client:
        c = self._client(client_id)
        c.status = status
        self.audit.record(action=f"client.{status}", resource_type="client", resource_id=c.id, actor="admin", client_id=c.id)
        self.db.commit()
        return c

    def _client(self, client_id: str) -> Client:
        c = self.clients.get(client_id)
        if c is None:
            raise NotFound("Client not found", code="client_not_found")
        return c

    def create_key(self, client_id: str, *, name: str, scopes: list[str] | None = None,
                   expires_in_days: int | None = None, rotated_from: str | None = None) -> tuple[ApiKey, str]:
        self._client(client_id)
        scopes = list(scopes or DEFAULT_SCOPES)
        if not set(scopes) <= VALID_SCOPES:
            raise BadRequest("Unknown scope", code="invalid_scope")
        g = generate_api_key(self.settings.api_key_pepper.get_secret_value())
        k = ApiKey(id=new_id("api_key"), client_id=client_id, name=name, prefix=g.prefix, key_hash=g.key_hash,
                   scopes=scopes, status="active", rotated_from_id=rotated_from,
                   expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None)
        self.db.add(k)
        self.audit.record(action="api_key.create", resource_type="api_key", resource_id=k.id, actor="admin",
                          client_id=client_id, details={"scopes": scopes, "rotated_from": rotated_from})
        self.db.commit()
        return k, g.full_key  # plaintext returned exactly once

    def revoke_key(self, client_id: str, key_id: str) -> ApiKey:
        k = self.keys.get_for_client(client_id, key_id)
        if k is None:
            raise NotFound("API key not found", code="api_key_not_found")
        k.status, k.revoked_at = "revoked", utcnow()
        self.audit.record(action="api_key.revoke", resource_type="api_key", resource_id=k.id, actor="admin", client_id=client_id)
        self.db.commit()
        return k

    def rotate_key(self, client_id: str, key_id: str, *, grace_seconds: int = 3600) -> tuple[ApiKey, str]:
        """Issue a replacement; the old key keeps working for a bounded grace period, then expires."""
        old = self.keys.get_for_client(client_id, key_id)
        if old is None or old.status != "active":
            raise NotFound("Active API key not found", code="api_key_not_found")
        new, full = self.create_key(client_id, name=old.name, scopes=old.scopes, rotated_from=old.id)
        old.expires_at = utcnow() + timedelta(seconds=max(0, min(grace_seconds, 7 * 86400)))
        self.audit.record(action="api_key.rotate", resource_type="api_key", resource_id=old.id, actor="admin",
                          client_id=client_id, details={"replaced_by": new.id, "grace_seconds": grace_seconds})
        self.db.commit()
        return new, full
