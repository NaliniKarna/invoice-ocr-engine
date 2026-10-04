"""Invoice review web layer: a small FastAPI app that serves the contributor pages and a handful of JSON endpoints.

It never talks to the database, object storage or queue; it calls the existing Bill OCR API with the collection client's
API key, held only here. Everything it returns to the browser is in plain language and uses opaque references."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.bodylimit import BodySizeLimitMiddleware
from app.webui import mapping
from app.webui.opaque import Opaque, OpaqueError
from app.webui.settings import UISettings
from app.webui.upstream import BillApi, UpstreamError

log = logging.getLogger("webui")
STATIC = Path(__file__).parent / "static"
_PID = re.compile(r"^[a-z0-9]{20}$")

_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"%PDF-", "application/pdf"))
_SERVE_TYPES = {m for _, m in _MAGIC}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; frame-src blob:; object-src blob:; "
       "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")

GENERIC = "Something went wrong. Please try again."
BUSY = "The service is busy. Please try again in a moment."
NOT_SET_UP = "This tool isn't set up correctly. Please tell the person who runs it."


def _msg(text: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"message": text, **extra}, status_code=status)


def _sniff(head: bytes) -> str | None:
    return next((mime for magic, mime in _MAGIC if head.startswith(magic)), None)


def _clean_name(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')[:100].strip()
    return base or "invoice"


class Submission(BaseModel):
    values: dict[str, str | None] = Field(default_factory=dict, max_length=60)
    items: list[dict[str, str | None]] = Field(default_factory=list, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


def create_app(settings: UISettings | None = None, http: httpx.Client | None = None) -> FastAPI:
    settings = settings or UISettings()  # type: ignore[call-arg]
    secret = settings.api_key.get_secret_value()
    http = http or httpx.Client(base_url=settings.api_base_url, timeout=settings.request_timeout_seconds)
    api, ids = BillApi(http, secret), Opaque(secret)

    app = FastAPI(title="Invoice review", docs_url=None, redoc_url=None, openapi_url=None)   # no Swagger, no schema
    max_body = settings.max_upload_bytes * settings.max_files_per_upload + 1024 * 1024
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)

    # ---------------------------------------------------------------- cross-cutting
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # No accounts means no cookies to ride on, but a page on another site could still make a contributor's browser
            # POST to this tool. Only accept same-origin (or explicitly allowed) browser requests.
            origin = request.headers.get("origin")
            same = origin is None or urlparse(origin).netloc == request.headers.get("host") or origin.rstrip("/") in settings.origins
            if request.headers.get("sec-fetch-site") == "cross-site" or not same:
                return _msg("This request was not allowed.", 403)
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
                             "X-Frame-Options": "DENY", "Content-Security-Policy": CSP})
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return _msg("Page not found." if exc.status_code == 404 else GENERIC, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad(_: Request, __: RequestValidationError):
        return _msg("That request wasn't understood.", 400)         # never echo submitted values

    @app.exception_handler(UpstreamError)
    async def _upstream(_: Request, exc: UpstreamError):
        return _upstream_message(exc)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return _msg(GENERIC, 500)

    def _upstream_message(exc: UpstreamError) -> JSONResponse:
        if exc.kind == "not_found":
            return _msg("This invoice wasn't found.", 404)
        if exc.kind == "conflict":
            return _msg("This invoice was already reviewed.", 409)
        if exc.kind == "misconfigured":
            return _msg(NOT_SET_UP, 503)
        if exc.kind == "busy":
            return _msg(BUSY, 503)
        return _msg(GENERIC, 502)

    def _pid(token: str) -> str:
        try:
            pid = ids.open(token)
        except OpaqueError:
            raise StarletteHTTPException(404) from None
        if not _PID.fullmatch(pid):
            raise StarletteHTTPException(404)
        return pid

    def _next_pending(exclude: str | None) -> str | None:
        try:
            rows = [i for i in api.list(limit=100, review="pending").get("items", []) if i["process_id"] != exclude]
        except UpstreamError:
            return None
        return ids.seal(rows[-1]["process_id"]) if rows else None      # oldest waiting invoice in the newest 100

    # ---------------------------------------------------------------- pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    # ---------------------------------------------------------------- JSON for the pages
    @app.get("/ui/config")
    def config():
        return {"notice": settings.feedback_notice.strip() or None, "max_mb": settings.max_upload_bytes // (1024 * 1024) or 1,
                "max_files": settings.max_files_per_upload, **mapping.field_spec()}

    @app.post("/ui/upload")
    def upload(files: list[UploadFile] = File(...)):
        if len(files) > settings.max_files_per_upload:
            return _msg(f"Please upload at most {settings.max_files_per_upload} invoices at a time.", 400)
        results = []
        for f in files:
            name = _clean_name(f.filename)
            data = f.file.read(settings.max_upload_bytes + 1)
            if len(data) > settings.max_upload_bytes:
                results.append({"name": name, "ok": False, "message": "This file is too large."})
                continue
            mime = _sniff(data[:16])
            if mime is None:
                results.append({"name": name, "ok": False, "message": "This file type isn't supported. Use PNG, JPG or PDF."})
                continue
            try:
                api.submit(name, data, mime)
                results.append({"name": name, "ok": True})
            except UpstreamError as exc:
                if exc.status == 413:
                    msg = "This file is too large."
                elif exc.status == 415:
                    msg = "This file type isn't supported. Use PNG, JPG or PDF."
                elif exc.kind == "busy":
                    msg = BUSY
                elif exc.kind == "misconfigured":
                    msg = NOT_SET_UP
                else:
                    msg = "This file couldn't be uploaded."
                results.append({"name": name, "ok": False, "message": msg})
        return {"results": results}

    @app.get("/ui/invoices")
    def invoices(cursor: str | None = None):
        raw = None
        if cursor:
            try:
                raw = ids.open(cursor)
            except OpaqueError:
                return _msg("That request wasn't understood.", 400)
        page = api.list(limit=50, cursor=raw)
        items = [{"id": ids.seal(i["process_id"]), "name": i.get("filename") or "Invoice",
                  "state": mapping.ui_state(i["status"], bool(i.get("reviewed"))),
                  "vendor": i.get("vendor"), "invoice_number": i.get("invoice_number"), "date": i.get("date"),
                  "total": mapping._num_text(i["total"], "money") if i.get("total") is not None else None,
                  "currency": i.get("currency")} for i in page.get("items", [])]
        return {"items": items, "more": ids.seal(page["next_cursor"]) if page.get("next_cursor") else None}

    @app.get("/ui/next")
    def next_invoice():
        return {"next": _next_pending(None)}

    @app.get("/ui/invoices/{token}")
    def invoice(token: str):
        pid = _pid(token)
        d = api.detail(pid)
        out = mapping.detail_payload(d, token, None)
        if out["state"] in ("ready", "reviewed"):
            out["next"] = _next_pending(pid)
        return out

    @app.get("/ui/invoices/{token}/file")
    def invoice_file(token: str):
        data, ctype = api.file(_pid(token))
        ctype = ctype.split(";")[0].strip().lower()
        if ctype not in _SERVE_TYPES:
            return _msg(GENERIC, 502)
        return Response(content=data, media_type=ctype, headers={"Content-Disposition": 'inline; filename="invoice"'})

    @app.post("/ui/invoices/{token}/submit")
    def submit(token: str, sub: Submission):
        pid = _pid(token)
        d = api.detail(pid)
        state = mapping.ui_state(d.get("status", ""), bool(d.get("reviewed")))
        if state == "reviewed":
            return _msg("This invoice was already reviewed.", 409)
        if state != "ready" or d.get("result") is None:
            return _msg("This invoice isn't ready to review yet.", 409)
        built = mapping.build_corrections(d["result"], sub.model_dump())
        failed = _msg("Your changes couldn't be submitted.", 422, fields=built.errors)
        if built.errors:
            return failed
        corrections = built.corrections
        for _ in range(2):
            try:
                api.review(pid, corrections, (sub.note or "").strip() or None)
                return {"ok": True, "next": _next_pending(pid)}
            except UpstreamError as exc:
                if exc.code == "already_reviewed":
                    return _msg("This invoice was already reviewed.", 409)
                if exc.code != "invalid_corrections":
                    return _upstream_message(exc)
                # A value that differs only in formatting is rejected by the API as "no change": drop it and retry.
                bad = {x.get("field"): x for x in exc.details}
                real = {f: mapping.GENERIC_CHECK for f, x in bad.items() if x.get("code") != "no_change"}
                if real:
                    return _msg("Your changes couldn't be submitted.", 422, fields=real)
                corrections = [c for c in corrections if c["field"] not in bad]
        return failed

    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.webui.main:app_factory
    return create_app()
