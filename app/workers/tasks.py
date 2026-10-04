from __future__ import annotations

import logging
from functools import lru_cache

from app.core.config import get_settings
from app.core.container import build_worker_context
from app.queue.base import CeleryJobQueue
from app.workers import maintenance
from app.workers.celery_app import celery_app
from app.workers.job import Outcome, WorkerContext, run_job

log = logging.getLogger(__name__)


@lru_cache
def _ctx() -> WorkerContext:
    return build_worker_context(get_settings())


@celery_app.task(bind=True, name="app.workers.tasks.process_document", max_retries=None)
def process_document(self, process_id: str) -> str:
    carrier = {k: v for k in ("traceparent", "tracestate") if isinstance(v := getattr(self.request, k, None), str)}
    result = run_job(process_id, _ctx(), carrier or None)
    if result.outcome in (Outcome.RETRY, Outcome.DEFERRED):
        # DB (attempts/max_attempts) is the source of truth for the retry budget, not Celery. DEFERRED does not
        # consume an attempt: the tenant was at its running-jobs cap.
        raise self.retry(countdown=result.countdown)
    return result.outcome.value


def _queue() -> CeleryJobQueue:
    import redis
    s = get_settings()
    return CeleryJobQueue(redis.Redis.from_url(s.redis_url), s)


@celery_app.task(name="app.workers.tasks.reap_stale_jobs")
def reap_stale_jobs() -> dict:
    return maintenance.reap_stale(_ctx(), _queue())


@celery_app.task(name="app.workers.tasks.purge_expired_documents")
def purge_expired_documents() -> int:
    return maintenance.purge_expired(_ctx())


@celery_app.task(name="app.workers.tasks.sweep_orphaned_objects")
def sweep_orphaned_objects() -> int:
    return maintenance.sweep_orphans(_ctx())


from celery.signals import worker_process_init  # noqa: E402


@worker_process_init.connect
def _warm_models(**_kwargs) -> None:
    """Load the production extractor once per worker process at start-up (not on the first customer document).
    Failure never prevents the worker from starting: the reference extractor is used and the reason is logged/metered."""
    try:
        _ctx().runtime.current()
        log.info("model runtime status", extra={"ml_status": _ctx().runtime.status()})
    except Exception:
        log.warning("model warm-up failed; reference extractor will be used", exc_info=False)
