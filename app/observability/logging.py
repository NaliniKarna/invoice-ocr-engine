"""Structured JSON logging with context and secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

from app.core.context import process_id_var, request_id_var, tenant_id_var

_API_KEY_RE = re.compile(r"ocr_[0-9a-f]{12}_[A-Za-z0-9_-]{20,}")
_SENSITIVE = {"authorization", "x-api-key", "x-admin-token", "password", "secret", "token",
              "api_key", "key_hash", "raw_text", "ocr_text"}
_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def redact(text: str) -> str:
    return _API_KEY_RE.sub("ocr_[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": request_id_var.get(),
            "tenant_id": tenant_id_var.get(),
            "process_id": process_id_var.get(),
        }
        for k, v in record.__dict__.items():
            if k in _STD or k.startswith("_"):
                continue
            payload[k] = "[REDACTED]" if k.lower() in _SENSITIVE else v
        if record.exc_info:
            # Stack traces go to logs only, never to clients.
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True  # we emit our own access log
