"""Safe model-artifact handling.

* Only data formats are accepted (JSON, safetensors, ONNX). Pickle-family formats (.pkl, .joblib, .pt, .pth, .bin, ...)
  can execute code when loaded and are refused by extension and by magic bytes.
* An artifact is trusted only if its SHA-256 matches the registry AND its HMAC-SHA256 signature verifies with the
  deployment's signing key (Settings.ml_model_signing_key). No key configured => no artifact-backed model is loaded.
* Size is capped before parsing.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from app.core.errors import Conflict

ALLOWED_EXTENSIONS = (".json", ".safetensors", ".onnx")
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
_PICKLE_MAGIC = (b"\x80\x02", b"\x80\x03", b"\x80\x04", b"\x80\x05", b"PK\x03\x04")   # pickle protocols 2-5, zip (torch)


class ArtifactError(Conflict):
    def __init__(self, reason: str):
        super().__init__("Model artifact rejected", code=f"artifact_{reason}")
        self.reason = reason


def sign(key: bytes, sha256_hex: str) -> str:
    return hmac.new(key, sha256_hex.encode(), hashlib.sha256).hexdigest()


ARTIFACT_PREFIX = "models/"


def check_uri(uri: str | None) -> None:
    if not uri or not uri.lower().endswith(ALLOWED_EXTENSIONS):
        raise ArtifactError("format_not_allowed")
    # Artifacts live only under models/. A registry row must never be able to make a worker read a tenant document,
    # a dataset or any other object, and the key must be traversal-proof.
    from app.core.errors import BadRequest
    from app.storage.base import validate_key
    try:
        validate_key(uri)
    except BadRequest as exc:
        raise ArtifactError("bad_location") from exc
    if not uri.startswith(ARTIFACT_PREFIX):
        raise ArtifactError("bad_location")


def verify(blob: bytes, *, uri: str | None, expected_sha256: str | None, signature: str | None, key: bytes) -> None:
    check_uri(uri)
    if not key:
        raise ArtifactError("no_signing_key")
    if len(blob) > MAX_ARTIFACT_BYTES:
        raise ArtifactError("too_large")
    if blob.startswith(_PICKLE_MAGIC):
        raise ArtifactError("unsafe_format")
    digest = hashlib.sha256(blob).hexdigest()
    if not expected_sha256 or not hmac.compare_digest(digest, expected_sha256):
        raise ArtifactError("checksum_mismatch")
    if not signature or not hmac.compare_digest(sign(key, digest), signature):
        raise ArtifactError("bad_signature")


def parse_json(blob: bytes) -> dict:
    if len(blob) > MAX_JSON_BYTES:
        raise ArtifactError("too_large")
    try:
        obj = json.loads(blob)
    except ValueError as exc:
        raise ArtifactError("malformed") from exc
    if not isinstance(obj, dict):
        raise ArtifactError("malformed")
    return obj
