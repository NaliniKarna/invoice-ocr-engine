"""Code identity for reproducibility. The repository ships without git metadata, so the source tree is fingerprinted."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

APP_VERSION = "0.3.0"


@lru_cache
def code_fingerprint() -> str:
    """SHA-256 over the relative path and content of every app/**/*.py (sorted). Changes whenever the code changes."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        h.update(str(f.relative_to(root)).encode()); h.update(f.read_bytes())
    return h.hexdigest()
