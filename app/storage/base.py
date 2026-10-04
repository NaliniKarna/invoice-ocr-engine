from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Protocol

from app.core.errors import BadRequest


class StorageError(Exception):
    """Transient or unexpected storage failure (retryable by workers)."""


class ObjectNotFound(StorageError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    last_modified: datetime
    size: int


class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> None: ...
    def list_objects(self, prefix: str) -> Iterator[ObjectInfo]: ...


def validate_key(key: str) -> str:
    """Keys are always server-generated; this is defence in depth against path traversal."""
    if (not key or len(key) > 512 or key.startswith("/") or "\\" in key or "\x00" in key
            or any(part in ("", ".", "..") for part in key.split("/"))):
        raise BadRequest("Invalid storage key")
    return key


def document_key(client_id: str, process_id: str, ext: str) -> str:
    return validate_key(f"tenants/{client_id}/documents/{process_id}/original.{ext}")


def ocr_artifact_key(client_id: str, process_id: str) -> str:
    return validate_key(f"tenants/{client_id}/documents/{process_id}/ocr.json")
