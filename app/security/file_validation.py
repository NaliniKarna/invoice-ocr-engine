"""Upload validation. Never trusts filename, Content-Type header, or extension.

Checks: size (streamed, capped), magic bytes, then structural content checks (app/security/content_checks.py)
executed in a resource-limited sandbox child so a hostile file cannot exhaust API memory/CPU.
Anything that cannot be parsed is rejected, not repaired.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from typing import BinaryIO

from app.core.errors import PayloadTooLarge, UnsupportedMedia, ValidationFailed
from app.security import sandbox
from app.security.content_checks import ContentRejected, Limits, check_image, check_pdf

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"%PDF-", "application/pdf", "pdf"),
)
_NAME_SAFE = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass(frozen=True)
class ValidatedFile:
    data: bytes
    content_type: str
    extension: str
    size: int
    sha256: str
    safe_filename: str | None
    page_count: int


def sanitize_filename(name: str | None) -> str | None:
    """Display-only. Never used to build storage paths."""
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _NAME_SAFE.sub("_", base).strip(" .")[:120]
    return base or None


def read_capped(stream: BinaryIO, max_bytes: int, chunk: int = 1024 * 1024) -> bytes:
    buf = bytearray()
    while True:
        part = stream.read(chunk)
        if not part:
            break
        buf += part
        if len(buf) > max_bytes:
            raise PayloadTooLarge(f"File exceeds the {max_bytes} byte limit", code="file_too_large")
    return bytes(buf)


def _sniff(data: bytes) -> tuple[str, str]:
    for sig, mime, ext in _SIGNATURES:
        if data.startswith(sig):
            return mime, ext
    raise UnsupportedMedia("Only PNG, JPEG and PDF files are accepted", code="unsupported_file_type")


def _content_check(data: bytes, mime: str, limits: Limits, sbx: sandbox.SandboxLimits | None) -> int:
    kind = "pdf" if mime == "application/pdf" else "image"
    if sbx is None:  # in-process (tests / explicit opt-out). Production default is sandboxed.
        try:
            return check_pdf(data, limits) if kind == "pdf" else (check_image(data, limits) or 1)
        except ContentRejected as exc:
            raise ValidationFailed(exc.message, code=exc.code) from exc
    try:
        raw = sandbox.run("app.security.validate_child", json.dumps({"kind": kind, "limits": asdict(limits)}), data, sbx)
        res = json.loads(raw.decode() or "{}")
    except sandbox.SandboxError as exc:
        raise ValidationFailed("Document is too complex to process safely", code="document_too_complex") from exc
    except ValueError as exc:
        raise ValidationFailed("Document could not be validated", code="invalid_file") from exc
    if not res.get("ok"):
        raise ValidationFailed(res.get("message", "Invalid file"), code=res.get("code", "invalid_file"))
    return int(res.get("pages", 1))


def validate_upload(stream: BinaryIO, *, filename: str | None, max_bytes: int, max_pdf_pages: int,
                    max_image_pixels: int, max_pdf_objects: int = 50_000, max_decoded_bytes: int = 64 * 1024 * 1024,
                    sandbox_limits: sandbox.SandboxLimits | None = None) -> ValidatedFile:
    data = read_capped(stream, max_bytes)
    if not data:
        raise ValidationFailed("File is empty", code="empty_file")
    mime, ext = _sniff(data)
    limits = Limits(max_image_pixels, max_pdf_pages, max_pdf_objects, max_decoded_bytes)
    pages = _content_check(data, mime, limits, sandbox_limits)
    return ValidatedFile(data, mime, ext, len(data), hashlib.sha256(data).hexdigest(),
                         sanitize_filename(filename), pages)
