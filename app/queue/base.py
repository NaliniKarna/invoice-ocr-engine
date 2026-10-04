from __future__ import annotations

from typing import Protocol

from app.core.config import Settings
from app.observability import tracing


class JobQueue(Protocol):
    def enqueue(self, process_id: str, *, bulk: bool = False) -> None: ...
    def depth(self, *, bulk: bool = False) -> int | None: ...  # None = unknown


class CeleryJobQueue:
    """Thin adapter so services never import Celery. Interactive and bulk work use separate queues so a large
    batch cannot sit in front of single-document requests (dedicated worker pools consume each)."""

    def __init__(self, redis_client, settings: Settings):
        self._redis = redis_client
        self._interactive, self._bulk = settings.queue_interactive, settings.queue_bulk

    def enqueue(self, process_id: str, *, bulk: bool = False) -> None:
        from app.workers.tasks import process_document
        q = self._bulk if bulk else self._interactive
        with tracing.span("queue.publish", queue=q):
            process_document.apply_async(args=[process_id], queue=q, headers=tracing.inject_headers() or None)

    def depth(self, *, bulk: bool = False) -> int | None:
        try:
            return int(self._redis.llen(self._bulk if bulk else self._interactive))
        except Exception:
            return None
