import hmac
import time

from fastapi import APIRouter, Depends, Header, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api.deps import get_container
from app.core.container import Container

router = APIRouter(include_in_schema=False)


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


_READY_TTL_S = 3.0
_ready_cache: dict[int, tuple[float, dict[str, str]]] = {}


def _db_ping(c: Container) -> None:
    with c.engine.connect() as conn:
        conn.execute(text("select 1"))


@router.get("/readyz")
def readyz(response: Response, c: Container = Depends(get_container)):
    """Readiness: cheap dependency pings only (never OCR). Result cached a few seconds so orchestrator
    probes cannot hammer PostgreSQL/Redis/S3."""
    now = time.monotonic()
    hit = _ready_cache.get(id(c))
    if hit and now - hit[0] < _READY_TTL_S:
        checks = hit[1]
    else:
        checks = {}
        for name, fn in (("database", lambda: _db_ping(c)), ("redis", lambda: c.redis.ping()), ("storage", c.storage.ping)):
            try:
                fn(); checks[name] = "ok"
            except Exception:
                checks[name] = "unavailable"
        _ready_cache[id(c)] = (now, checks)
    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = 503
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/metrics")
def metrics(c: Container = Depends(get_container), authorization: str | None = Header(default=None)):
    tok = c.settings.metrics_token
    if tok is None:
        if c.settings.env == "prod":
            return Response(status_code=404)
    elif not authorization or not hmac.compare_digest(authorization, f"Bearer {tok.get_secret_value()}"):
        return Response(status_code=401)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
