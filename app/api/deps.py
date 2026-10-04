"""FastAPI dependencies: composition-root access, authentication, scope checks, rate limits."""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Callable

from fastapi import Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.container import Container
from app.core.context import tenant_id_var
from app.core.errors import BadRequest, Forbidden, NotFound, Unauthorized, ValidationFailed
from app.core.ids import is_valid_id
from app.core.time import as_utc, utcnow
from app.db.session import session_scope
from app.observability.metrics import AUTH_FAILURES
from app.repositories.tenancy import ApiKeyRepository
from app.security.api_keys import parse_prefix, verify_key
from app.services.principal import Principal

_IDEM_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
TOUCH_INTERVAL_S = 60


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_settings_dep(c: Container = Depends(get_container)) -> Settings:
    return c.settings


def get_db(c: Container = Depends(get_container)) -> Iterator[Session]:
    yield from session_scope(c.session_factory)


def client_ip(request: Request, settings: Settings) -> str:
    hops = settings.trusted_proxy_hops
    if hops > 0:
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]  # rightmost-trusted: entries further left are client-controlled
    return request.client.host if request.client else "unknown"


def _fail(reason: str, c: Container, ip: str, exc: Exception) -> None:
    AUTH_FAILURES.labels(reason=reason).inc()
    c.limiter.enforce("authfail", ip, limit=c.settings.auth_failures_per_minute,
                      message="Too many failed authentication attempts")
    raise exc


def authenticate(request: Request, response: Response, db: Session = Depends(get_db),
                 c: Container = Depends(get_container),
                 x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Principal:
    """API key -> key row -> client -> plan. Tenant identity comes ONLY from here."""
    ip = client_ip(request, c.settings)
    c.limiter.enforce("ip", ip, limit=c.settings.ip_requests_per_minute)

    prefix = parse_prefix(x_api_key)
    key = ApiKeyRepository(db).by_prefix(prefix) if prefix else None
    pepper = c.settings.api_key_pepper.get_secret_value()
    ok = verify_key(x_api_key or "", key.key_hash if key else None, pepper)
    now = utcnow()
    if not ok or key is None:
        _fail("invalid", c, ip, Unauthorized("Invalid or missing API key"))
    if key.status != "active":
        _fail("revoked", c, ip, Unauthorized("Invalid or missing API key"))
    if key.expires_at is not None and as_utc(key.expires_at) <= now:
        _fail("expired", c, ip, Unauthorized("Invalid or missing API key"))
    client = key.client
    if client.status != "active" or client.deleted_at is not None:
        raise Forbidden("Account is not active", code="account_suspended")

    tenant_id_var.set(client.id)
    request.state.tenant_id = client.id  # context vars set in threadpool deps don't reach the middleware
    plan = client.plan
    lim = c.limiter.enforce("key", key.id, limit=plan.requests_per_minute)
    c.limiter.enforce("tenant", client.id, limit=plan.tenant_requests_per_minute)
    # Stored on request.state: headers set on the injected Response are lost when a handler raises.
    request.state.rl_headers = {"X-RateLimit-Limit": str(lim.limit), "X-RateLimit-Remaining": str(lim.remaining)}

    if key.last_used_at is None or (now - as_utc(key.last_used_at)).total_seconds() > TOUCH_INTERVAL_S:
        ApiKeyRepository(db).touch(key.id, now)
        db.commit()
    return Principal(client=client, plan=plan, api_key_id=key.id, scopes=frozenset(key.scopes or []), ip=ip)


def require_scope(scope: str) -> Callable[..., Principal]:
    def dep(p: Principal = Depends(authenticate)) -> Principal:
        if scope not in p.scopes:
            raise Forbidden(f"API key lacks the '{scope}' scope", code="insufficient_scope")
        return p
    return dep


def upload_rate_limit(p: Principal = Depends(require_scope("bills:write")),
                      c: Container = Depends(get_container)) -> Principal:
    c.limiter.enforce("upload", p.client_id, limit=p.plan.upload_requests_per_minute,
                      message="Upload rate limit exceeded")
    return p


def idempotency_key(idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> str | None:
    if idempotency_key is None:
        return None
    if not _IDEM_RE.match(idempotency_key):
        raise ValidationFailed("Idempotency-Key must be 8-128 chars of [A-Za-z0-9_-:.]", code="invalid_idempotency_key")
    return idempotency_key


def process_id_param(process_id: str) -> str:
    if not is_valid_id("process", process_id):
        raise NotFound("Process not found", code="process_not_found")
    return process_id


def batch_id_param(batch_id: str) -> str:
    if not is_valid_id("batch", batch_id):
        raise NotFound("Batch not found", code="batch_not_found")
    return batch_id


def bulk_rate_limit(p: Principal = Depends(upload_rate_limit), c: Container = Depends(get_container)) -> Principal:
    """Bulk endpoint additionally consumes the (much smaller) bulk bucket."""
    c.limiter.enforce("bulk", p.client_id, limit=p.plan.bulk_requests_per_minute, message="Bulk rate limit exceeded")
    return p
