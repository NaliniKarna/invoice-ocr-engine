from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.core.errors import ValidationFailed


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[dict] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody

    model_config = {"json_schema_extra": {"example": {"error": {
        "code": "unsupported_file_type", "message": "Only PNG, JPEG and PDF files are accepted",
        "request_id": "req_0f3c9a7e5b1d4c2e8a6b7d9e0f1a2b3c"}}}}


def parse_metadata(raw: str | None, max_bytes: int) -> dict[str, Any]:
    """Client metadata: flat JSON object of scalars, bounded. Stored opaque, never interpreted."""
    if raw is None or raw == "":
        return {}
    if len(raw.encode()) > max_bytes:
        raise ValidationFailed("metadata too large", code="metadata_too_large")
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise ValidationFailed("metadata must be valid JSON", code="invalid_metadata") from exc
    if not isinstance(obj, dict) or len(obj) > 20:
        raise ValidationFailed("metadata must be an object with at most 20 keys", code="invalid_metadata")
    for k, v in obj.items():
        if not isinstance(k, str) or len(k) > 64 or not (v is None or isinstance(v, (str, int, float, bool))):
            raise ValidationFailed("metadata values must be scalars", code="invalid_metadata")
        if isinstance(v, str) and len(v) > 512:
            raise ValidationFailed("metadata string too long", code="invalid_metadata")
    return obj
