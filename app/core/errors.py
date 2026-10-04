"""Domain/HTTP errors. Messages here are safe to show to clients."""
from __future__ import annotations


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str = "Internal error", *, code: str | None = None,
                 headers: dict[str, str] | None = None, details: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.headers = headers or {}
        self.details = details


class Unauthorized(AppError):
    status_code, code = 401, "invalid_api_key"


class Forbidden(AppError):
    status_code, code = 403, "forbidden"


class NotFound(AppError):
    status_code, code = 404, "not_found"


class Conflict(AppError):
    status_code, code = 409, "conflict"


class ValidationFailed(AppError):
    status_code, code = 422, "validation_failed"


class BadRequest(AppError):
    status_code, code = 400, "bad_request"


class PayloadTooLarge(AppError):
    status_code, code = 413, "payload_too_large"


class UnsupportedMedia(AppError):
    status_code, code = 415, "unsupported_media_type"


class RateLimited(AppError):
    status_code, code = 429, "rate_limited"

    def __init__(self, message: str, *, retry_after: int, code: str | None = None,
                 headers: dict[str, str] | None = None):
        h = {"Retry-After": str(max(1, int(retry_after)))}
        h.update(headers or {})
        super().__init__(message, code=code, headers=h)


class ServiceUnavailable(AppError):
    status_code, code = 503, "service_unavailable"
