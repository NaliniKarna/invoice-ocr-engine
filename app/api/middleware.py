from __future__ import annotations

import logging
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.context import process_id_var, request_id_var, tenant_id_var
from app.observability import tracing
from app.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

log = logging.getLogger("access")
_RID_RE = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}

# Swagger UI (/docs) needs its script, stylesheet and an inline bootstrap script from a CDN. It is served
# only when OCR_ENV is not "prod" (docs_url is None in prod), and only this one path gets the looser policy.
# Every API response keeps the strict "default-src 'none'" policy above.
DOCS_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'; frame-ancestors 'none'")


async def request_context(request: Request, call_next):
    inbound = request.headers.get("x-request-id", "")
    rid = inbound if _RID_RE.match(inbound) else f"req_{uuid.uuid4().hex}"  # never echo unvalidated input
    request_id_var.set(rid)
    tenant_id_var.set(None)
    process_id_var.set(None)
    start = time.perf_counter()

    max_body = request.app.state.container.settings.max_request_bytes
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_body:
        resp = JSONResponse({"error": {"code": "payload_too_large", "message": "Request body too large",
                                       "request_id": rid}}, status_code=413)
    else:
        with tracing.span("http.request", method=request.method, request_id=rid):
            resp = await call_next(request)

    route = getattr(request.scope.get("route"), "path", "unmatched")
    dur = time.perf_counter() - start
    HTTP_REQUESTS.labels(request.method, route, str(resp.status_code)).inc()
    HTTP_LATENCY.labels(request.method, route).observe(dur)
    resp.headers["X-Request-ID"] = rid
    for k, v in getattr(request.state, "rl_headers", {}).items():
        resp.headers.setdefault(k, v)
    if request.url.path == "/docs" and request.app.state.container.settings.env != "prod":
        resp.headers["Content-Security-Policy"] = DOCS_CSP
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    tenant_id_var.set(getattr(request.state, "tenant_id", None))
    log.info("request", extra={"method": request.method, "route": route, "status": resp.status_code,
                               "duration_ms": int(dur * 1000)})
    return resp