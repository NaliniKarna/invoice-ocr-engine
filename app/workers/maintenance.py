"""Periodic recovery + retention jobs (run by Celery beat; safe to run concurrently)."""
from __future__ import annotations

import logging
from datetime import timedelta

from app.core.time import utcnow
from app.observability import metrics as m
from app.queue.base import JobQueue
from app.repositories.process import ProcessRepository
from app.services import state
from app.services.processes import purge_process
from app.storage.base import StorageError
from app.workers.job import WorkerContext

log = logging.getLogger(__name__)


def reap_stale(ctx: WorkerContext, queue: JobQueue, limit: int = 500) -> dict[str, int]:
    """Fixes the DB-commit/enqueue dual-write gap and jobs orphaned by crashed workers."""
    db = ctx.session_factory()
    stats = {"requeued": 0, "recovered": 0, "failed": 0}
    try:
        procs = ProcessRepository(db)
        now = utcnow()
        ids = procs.stale_queued(now - timedelta(seconds=ctx.settings.stale_queued_seconds), limit)
        bulk_ids = procs.bulk_ids(ids)
        done = []
        for pid in ids:
            try:
                queue.enqueue(pid, bulk=pid in bulk_ids); done.append(pid); stats["requeued"] += 1
            except Exception:
                log.exception("re-enqueue failed")
        procs.mark_enqueued(done)
        for proc in procs.stale_processing(now - timedelta(seconds=ctx.settings.stale_processing_seconds), limit):
            if proc.attempts < proc.max_attempts:
                if procs.transition(proc.id, state.QUEUED, sources=[state.PROCESSING], error_code="worker_lost"):
                    db.commit()
                    try:
                        queue.enqueue(proc.id, bulk=proc.batch_id is not None); procs.mark_enqueued([proc.id])
                    except Exception:
                        log.exception("re-enqueue failed")
                    stats["recovered"] += 1
            elif procs.transition(proc.id, state.FAILED, sources=[state.PROCESSING], completed_at=now,
                                  error_code="worker_timeout", error_message="Processing timed out",
                                  error_retryable=False):
                stats["failed"] += 1
        db.commit()
        for k, v in stats.items():
            if v:
                m.REAPED.labels(kind=k).inc(v)
        return stats
    finally:
        db.close()


def purge_expired(ctx: WorkerContext, limit: int = 200) -> int:
    from app.models import Client, IdempotencyKey
    db = ctx.session_factory()
    n = 0
    try:
        procs = ProcessRepository(db)
        for proc in procs.expired(utcnow(), limit):
            client = db.get(Client, proc.client_id)
            try:
                purge_process(db, ctx.storage, proc, hard_feedback=not (client and client.training_opt_in))
                db.commit(); n += 1
            except Exception:
                db.rollback(); log.exception("purge failed")
        db.query(IdempotencyKey).filter(IdempotencyKey.expires_at < utcnow()).delete()
        db.commit()
        purge_audit_logs(ctx)
        return n
    finally:
        db.close()


def purge_audit_logs(ctx: WorkerContext, batch: int = 1000) -> int:
    """Delete audit rows older than `audit_retention_days` (bounded per run so the delete never holds long locks)."""
    from sqlalchemy import select
    from app.models import AuditLog
    cutoff = utcnow() - timedelta(days=ctx.settings.audit_retention_days)
    db = ctx.session_factory()
    try:
        ids = select(AuditLog.id).where(AuditLog.created_at < cutoff).limit(batch)
        n = db.query(AuditLog).filter(AuditLog.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return n
    finally:
        db.close()


def sweep_orphans(ctx: WorkerContext, grace: timedelta = timedelta(hours=2), max_scan: int = 5000) -> int:
    """Delete objects under tenants/ whose process row is missing or soft-deleted (failed-upload leftovers, crashed
    purges). Only objects older than `grace` are considered, so in-flight uploads (object written before the row) are safe."""
    from sqlalchemy import select
    from app.models import Process
    from app.observability.metrics import ORPHANS_SWEPT
    cutoff = utcnow() - grace
    candidates: dict[str, str] = {}   # key -> process_id
    try:
        for n, obj in enumerate(ctx.storage.list_objects("tenants/")):
            if n >= max_scan:
                break
            parts = obj.key.split("/")
            if len(parts) == 5 and parts[2] == "documents" and obj.last_modified < cutoff:
                candidates[obj.key] = parts[3]
    except StorageError:
        log.exception("orphan sweep: listing failed")
        return 0
    if not candidates:
        return 0
    db = ctx.session_factory()
    try:
        ids = list(set(candidates.values()))
        live: set[str] = set()
        for i in range(0, len(ids), 500):
            live |= set(db.scalars(select(Process.id).where(Process.id.in_(ids[i:i + 500]), Process.deleted_at.is_(None))))
        n = 0
        for key, pid in candidates.items():
            if pid not in live:
                try:
                    ctx.storage.delete(key); n += 1
                except StorageError:
                    log.warning("orphan delete failed", extra={"key": key})
        ORPHANS_SWEPT.inc(n)
        return n
    finally:
        db.close()
