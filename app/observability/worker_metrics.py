"""Prometheus exposition for Celery prefork workers.

Children increment metrics in their own memory, so the multiprocess client is REQUIRED:
set PROMETHEUS_MULTIPROC_DIR (a writable tmpfs dir) in the worker environment *before Python starts*.
The worker's main process serves the aggregate on OCR_WORKER_METRICS_PORT (default 9100) at /metrics.
Bind that port to the private network only; it is unauthenticated.
"""
from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger(__name__)


def prepare_dir() -> str | None:
    d = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not d:
        return None
    shutil.rmtree(d, ignore_errors=True)   # stale files from a previous container run would be re-aggregated
    os.makedirs(d, exist_ok=True)
    return d


def start_exposition(port: int) -> bool:
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        log.warning("PROMETHEUS_MULTIPROC_DIR not set: worker metrics are NOT exported")
        return False
    from prometheus_client import CollectorRegistry, multiprocess, start_http_server
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    start_http_server(port, registry=registry)
    return True


def mark_dead(pid: int) -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess
        multiprocess.mark_process_dead(pid)
