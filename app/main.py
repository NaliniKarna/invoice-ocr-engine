from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError, TimeoutError as SATimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import health
from app.api.bodylimit import BodySizeLimitMiddleware
from app.api.internal import admin, ml as internal_ml
from app.api.middleware import request_context
from app.api.v1 import bills
from app.core.config import Settings, get_settings
from app.core.container import Container, build_container
from app.core.context import request_id_var
from app.core.errors import AppError
from app.observability import tracing
from app.observability.logging import configure_logging
from app.storage.base import StorageError

log = logging.getLogger(__name__)


def _err(status: int, code: str, message: str, headers=None, details=None) -> JSONResponse:
    body = {"code": code, "message": message, "request_id": request_id_var.get()}
    if details:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def create_app(container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())
    configure_logging()
    app = FastAPI(title="Bill OCR API", version="0.3.0",
                  description="Asynchronous bill/invoice extraction. Authenticate every call with the `X-API-Key` header. "
                              "All errors use `{error:{code,message,request_id}}`; quote `request_id` (also returned as the "
                              "`X-Request-ID` header) to support. Rate limits: HTTP 429 + `Retry-After`; `X-RateLimit-*` headers on "
                              "authenticated responses.", docs_url=None if settings.env == "prod" else "/docs",
                  redoc_url=None, openapi_url=None if settings.env == "prod" else "/openapi.json")
    app.state.container = container or build_container(settings)
    tracing.setup_tracing(enabled=settings.otel_enabled, service_name=settings.service_name, app=app,
                          engine=app.state.container.engine)

    if settings.cors_allow_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_allow_origins, allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["X-API-Key", "Idempotency-Key", "Content-Type", "X-Request-ID"], allow_credentials=False)
    app.middleware("http")(request_context)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)  # outermost

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        if exc.status_code >= 500:
            log.error("server error: %s", exc.code)
        return _err(exc.status_code, exc.code, exc.message, exc.headers, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        # Report location+message only; never echo submitted values.
        details = [{"loc": [str(x) for x in e.get("loc", [])], "message": e.get("msg", "invalid")} for e in exc.errors()]
        return _err(422, "validation_failed", "Request validation failed", details=details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return _err(exc.status_code, {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error"),
                    "Resource not found" if exc.status_code == 404 else "Request error")

    @app.exception_handler(OperationalError)
    @app.exception_handler(SATimeoutError)
    async def _db_down(_: Request, exc: Exception):
        # Connection loss, pool exhaustion, statement/lock timeouts: durable work did NOT happen -> never 2xx.
        log.error("database unavailable", extra={"exc_type": type(exc).__name__})
        return _err(503, "database_unavailable", "Service temporarily unavailable, retry later",
                    headers={"Retry-After": "5"})

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: Exception):
        log.error("database error", extra={"exc_type": type(exc).__name__})  # SQL text is never returned
        return _err(500, "internal_error", "An internal error occurred. Quote the request_id when contacting support.")

    @app.exception_handler(StorageError)
    async def _storage_down(_: Request, exc: Exception):
        log.error("storage error", extra={"exc_type": type(exc).__name__})
        return _err(503, "storage_unavailable", "Storage temporarily unavailable, retry later",
                    headers={"Retry-After": "10"})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled exception")  # stack trace to logs only
        return _err(500, "internal_error", "An internal error occurred. Quote the request_id when contacting support.")

    app.include_router(health.router)
    app.include_router(bills.router)
    app.include_router(admin.router)
    app.include_router(internal_ml.router)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "ApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            "AdminToken": {"type": "apiKey", "in": "header", "name": "X-Admin-Token"}}
        for path, ops in schema["paths"].items():
            for op in ops.values():
                op["security"] = [{"AdminToken": []}] if path.startswith("/internal/") else [{"ApiKey": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def app_factory() -> FastAPI:  # uvicorn --factory app.main:app_factory
    return create_app()
