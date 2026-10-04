"""API key generation and verification.

Format: ocr_<12 hex prefix>_<43 char urlsafe secret>  (256 bits of entropy in the secret).
Storage: HMAC-SHA256(server pepper, full key). Because the secret is high-entropy random,
a fast keyed hash is appropriate (slow password hashes protect low-entropy human secrets and
would make every request expensive). The pepper lives in a secret manager, not the DB, so a
DB leak alone cannot be used to verify guesses. Lookup is by the non-secret prefix.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

_KEY_RE = re.compile(r"^ocr_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
VALID_SCOPES = frozenset({"bills:write", "bills:read", "feedback:write"})
DEFAULT_SCOPES = ("bills:write", "bills:read", "feedback:write")


@dataclass(frozen=True)
class GeneratedKey:
    full_key: str  # shown once, never stored
    prefix: str
    key_hash: str


def hash_key(full_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), full_key.encode(), hashlib.sha256).hexdigest()


def generate_api_key(pepper: str) -> GeneratedKey:
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    full = f"ocr_{prefix}_{secret}"
    return GeneratedKey(full, prefix, hash_key(full, pepper))


def parse_prefix(presented: str | None) -> str | None:
    if not presented or len(presented) > 100:
        return None
    m = _KEY_RE.match(presented)
    return m.group(1) if m else None


def verify_key(presented: str, stored_hash: str | None, pepper: str) -> bool:
    """Constant-time. When no row exists, pass stored_hash=None: a dummy compare keeps timing flat."""
    candidate = hash_key(presented, pepper)
    return hmac.compare_digest(candidate, stored_hash or "0" * 64) and stored_hash is not None
