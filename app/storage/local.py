from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.storage.base import ObjectInfo, ObjectNotFound, StorageError, validate_key


class LocalFsStorage:
    """Dev/test only (rejected in prod by Settings)."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / validate_key(key)).resolve()
        if os.path.commonpath([self.root, p]) != str(self.root):
            raise StorageError("path escape")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFound(key) from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def ping(self) -> None:
        if not self.root.exists():
            raise StorageError("root missing")

    def list_objects(self, prefix: str) -> Iterator[ObjectInfo]:
        base = self._path(prefix.rstrip("/") or ".") if prefix else self.root
        if not base.exists():
            return
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.name.endswith(".tmp"):
                st = f.stat()
                yield ObjectInfo(f.relative_to(self.root).as_posix(),
                                 datetime.fromtimestamp(st.st_mtime, timezone.utc), st.st_size)
