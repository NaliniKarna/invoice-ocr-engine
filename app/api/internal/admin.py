"""Operator-only API. MUST NOT be exposed on the public internet (bind to a private listener/VPN).
Authenticated by a separate static admin token, compared in constant time."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import client_ip, get_container, get_db
from app.core.container import Container
from app.core.errors import Unauthorized
from app.observability.metrics import AUTH_FAILURES
from app.security.api_keys import DEFAULT_SCOPES
from app.services.admin import AdminService


def admin_auth(request: Request, c: Container = Depends(get_container),
               x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
    expected = c.settings.admin_token.get_secret_value()
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), expected.encode()):
        # Same per-IP failed-authentication budget as tenant keys, so the operator token cannot be guessed at line speed.
        AUTH_FAILURES.labels(reason="admin_invalid").inc()
        c.limiter.enforce("authfail", client_ip(request, c.settings), limit=c.settings.auth_failures_per_minute,
                          message="Too many failed authentication attempts")
        raise Unauthorized("Invalid admin token", code="invalid_admin_token")


router = APIRouter(prefix="/internal/v1", tags=["internal-admin"], dependencies=[Depends(admin_auth)])


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan_id: str = "free"
    training_opt_in: bool = False
    training_policy: str | None = Field(default=None, pattern="^(none|tenant_only|pooled)$")


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class RotateBody(BaseModel):
    grace_seconds: int = Field(default=3600, ge=0, le=7 * 86400)


def _key_out(k, full: str | None = None) -> dict:
    out = {"key_id": k.id, "client_id": k.client_id, "name": k.name, "prefix": k.prefix, "scopes": k.scopes,
           "status": k.status, "expires_at": k.expires_at}
    if full:
        out["api_key"] = full  # returned exactly once
    return out


@router.post("/clients", status_code=201)
def create_client(body: ClientCreate, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    cl = AdminService(db, c.settings).create_client(name=body.name, plan_id=body.plan_id, training_opt_in=body.training_opt_in,
                                                                        training_policy=body.training_policy)
    return {"client_id": cl.id, "name": cl.name, "plan_id": cl.plan_id, "status": cl.status}


@router.post("/clients/{client_id}/suspend")
def suspend(client_id: str, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    cl = AdminService(db, c.settings).set_status(client_id, "suspended")
    return {"client_id": cl.id, "status": cl.status}


@router.post("/clients/{client_id}/activate")
def activate(client_id: str, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    cl = AdminService(db, c.settings).set_status(client_id, "active")
    return {"client_id": cl.id, "status": cl.status}


@router.post("/clients/{client_id}/api-keys", status_code=201)
def create_key(client_id: str, body: KeyCreate, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    k, full = AdminService(db, c.settings).create_key(client_id, name=body.name, scopes=body.scopes,
                                                      expires_in_days=body.expires_in_days)
    return _key_out(k, full)


@router.post("/clients/{client_id}/api-keys/{key_id}/revoke")
def revoke_key(client_id: str, key_id: str, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    return _key_out(AdminService(db, c.settings).revoke_key(client_id, key_id))


@router.post("/clients/{client_id}/api-keys/{key_id}/rotate", status_code=201)
def rotate_key(client_id: str, key_id: str, body: RotateBody, db: Session = Depends(get_db),
               c: Container = Depends(get_container)):
    k, full = AdminService(db, c.settings).rotate_key(client_id, key_id, grace_seconds=body.grace_seconds)
    return _key_out(k, full)
