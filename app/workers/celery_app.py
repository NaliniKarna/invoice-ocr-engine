from __future__ import annotations

from app.observability import worker_metrics

# Must run before anything imports app.observability.metrics (multiprocess mode needs the dir to exist).
worker_metrics.prepare_dir()

from celery import Celery  # noqa: E402
from celery.signals import setup_logging, worker_process_init, worker_process_shutdown, worker_ready  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.observability.logging import configure_logging  # noqa: E402

_s = get_settings()
celery_app = Celery("bill_ocr", broker=_s.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",  # never pickle
    task_ignore_result=True,                     # Postgres is the system of record
    task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
    task_time_limit=_s.job_time_limit_seconds, task_soft_time_limit=_s.job_time_limit_seconds - 30,
    broker_transport_options={"visibility_timeout": _s.job_time_limit_seconds * 3},
    task_default_queue=_s.queue_interactive,
    task_routes={"app.workers.tasks.process_document": {"queue": _s.queue_interactive}},
    broker_connection_retry_on_startup=True, timezone="UTC",
    beat_schedule={
        "reap-stale-jobs": {"task": "app.workers.tasks.reap_stale_jobs", "schedule": 60.0,
                            "options": {"queue": _s.queue_interactive}},
        "purge-expired": {"task": "app.workers.tasks.purge_expired_documents", "schedule": 3600.0,
                          "options": {"queue": _s.queue_interactive}},
        "sweep-orphans": {"task": "app.workers.tasks.sweep_orphaned_objects", "schedule": 6 * 3600.0,
                          "options": {"queue": _s.queue_interactive}},
    },
)


@setup_logging.connect
def _json_logging(**_kwargs) -> None:
    """Stop Celery from installing its own plain-text handlers; keep structured JSON + redaction."""
    configure_logging()


@worker_ready.connect
def _start_metrics(**_kwargs) -> None:
    worker_metrics.start_exposition(_s.worker_metrics_port)


@worker_process_init.connect
def _child_up(**_kwargs) -> None:
    from app.observability import tracing
    from app.observability.metrics import WORKER_PROCESSES
    WORKER_PROCESSES.inc()
    tracing.setup_tracing(enabled=_s.otel_enabled, service_name=f"{_s.service_name}-worker")


@worker_process_shutdown.connect
def _child_down(pid=None, **_kwargs) -> None:
    if pid:
        worker_metrics.mark_dead(pid)
