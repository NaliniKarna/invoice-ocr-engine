"""Reversible, tamper-evident tokens for everything the browser must hold on to (invoice references, list cursors).

The browser should never receive the API's process ids or cursor internals. A token is `b64url(tag || ciphertext)`, where
tag = HMAC(key, text)[:8] and the ciphertext is the text XOR an HMAC-derived keystream seeded by the tag (a synthetic-IV
construction). It is deterministic, stateless, and rejects any modified token. It hides identifiers from the page; it is
not a replacement for access control (the API key and tenant scoping stay on the server)."""
from __future__ import annotations

import base64
import hashlib
import hmac

_TAG = 8


class OpaqueError(ValueError):
    pass


class Opaque:
    def __init__(self, secret: str):
        self._key = hmac.new(secret.encode(), b"bill-ocr-ui/opaque/v1", hashlib.sha256).digest()

    def _tag(self, data: bytes) -> bytes:
        return hmac.new(self._key, b"t" + data, hashlib.sha256).digest()[:_TAG]

    def _stream(self, tag: bytes, n: int) -> bytes:
        out, i = b"", 0
        while len(out) < n:
            out += hmac.new(self._key, b"s" + tag + i.to_bytes(4, "big"), hashlib.sha256).digest()
            i += 1
        return out[:n]

    def seal(self, text: str) -> str:
        data = text.encode()
        tag = self._tag(data)
        ct = bytes(a ^ b for a, b in zip(data, self._stream(tag, len(data))))
        return base64.urlsafe_b64encode(tag + ct).decode().rstrip("=")

    def open(self, token: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            if len(raw) <= _TAG or len(raw) > 512:
                raise ValueError
            tag, ct = raw[:_TAG], raw[_TAG:]
            data = bytes(a ^ b for a, b in zip(ct, self._stream(tag, len(ct))))
            if not hmac.compare_digest(self._tag(data), tag):
                raise ValueError
            return data.decode()
        except Exception:
            raise OpaqueError("invalid token") from None
