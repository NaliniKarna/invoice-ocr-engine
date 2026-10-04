"""Every query is tenant-scoped by construction: client_id is a required argument and part of
the WHERE clause. There is deliberately no `get(process_id)` without a tenant."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models import Batch, OcrResult, Process
from app.services import state


class ProcessRepository:
    def __init__(self, db: Session):
        self.db = db

    # ---- tenant-scoped reads
    def get(self, client_id: str, process_id: str, *, include_deleted: bool = False) -> Process | None:
        q = select(Process).where(Process.client_id == client_id, Process.id == process_id)
        if not include_deleted:
            q = q.where(Process.deleted_at.is_(None))
        return self.db.execute(q).scalar_one_or_none()

    def get_result(self, client_id: str, process_id: str) -> OcrResult | None:
        return self.db.execute(select(OcrResult).where(
            OcrResult.client_id == client_id, OcrResult.process_id == process_id)).scalar_one_or_none()

    def get_batch(self, client_id: str, batch_id: str) -> Batch | None:
        return self.db.execute(select(Batch).where(Batch.client_id == client_id, Batch.id == batch_id)).scalar_one_or_none()

    def list_batch(self, client_id: str, batch_id: str) -> list[Process]:
        return list(self.db.execute(select(Process).where(
            Process.client_id == client_id, Process.batch_id == batch_id, Process.deleted_at.is_(None))
            .order_by(Process.created_at, Process.id)).scalars())

    def count_active(self, client_id: str) -> int:
        return self.db.execute(select(func.count()).select_from(Process).where(
            Process.client_id == client_id, Process.status.in_(state.ACTIVE),
            Process.deleted_at.is_(None))).scalar_one()

    def count_created_since(self, client_id: str, since: datetime) -> int:
        # Deleted rows still count toward quota (deleting must not refund usage).
        return self.db.execute(select(func.count()).select_from(Process).where(
            Process.client_id == client_id, Process.created_at >= since)).scalar_one()

    # ---- writes
    def add(self, obj) -> None:
        self.db.add(obj)

    def transition(self, process_id: str, dst: str, *, sources: list[str] | None = None,
                   attempt: int | None = None, **values) -> bool:
        """Atomic CAS. Returns True if this caller performed the transition.
        `attempt` is a fencing token: a worker passes the attempt number it claimed, so a slow worker whose job
        was re-queued and re-claimed by someone else can no longer complete or fail it."""
        srcs = sources or state.sources_for(dst)
        cond = [Process.id == process_id, Process.status.in_(srcs)]
        if attempt is not None:
            cond.append(Process.attempts == attempt)
        res = self.db.execute(update(Process).where(*cond).values(status=dst, updated_at=utcnow(), **values))
        return res.rowcount == 1

    def claim(self, process_id: str) -> int | None:
        """queued -> processing. Returns the new attempt number (fencing token) or None if not claimable."""
        res = self.db.execute(update(Process).where(Process.id == process_id, Process.status == state.QUEUED,
                                                    Process.deleted_at.is_(None))
                              .values(status=state.PROCESSING, started_at=utcnow(), updated_at=utcnow(),
                                      attempts=Process.attempts + 1).returning(Process.attempts))
        row = res.first()
        return row[0] if row else None

    def count_running(self, client_id: str, *, started_after: datetime) -> int:
        """Jobs of one tenant executing now. Rows older than `started_after` are presumed dead (awaiting the reaper)
        and do not count, so crashed workers cannot block a tenant until recovery."""
        return self.db.execute(select(func.count()).select_from(Process).where(
            Process.client_id == client_id, Process.status == state.PROCESSING,
            Process.started_at >= started_after)).scalar_one()

    def oldest_created_since(self, client_id: str, since: datetime) -> datetime | None:
        return self.db.execute(select(func.min(Process.created_at)).where(
            Process.client_id == client_id, Process.created_at >= since)).scalar_one()

    def mark_enqueued(self, process_ids: list[str]) -> None:
        if process_ids:
            self.db.execute(update(Process).where(Process.id.in_(process_ids)).values(enqueued_at=utcnow()))

    def stale_queued(self, older_than: datetime, limit: int) -> list[str]:
        q = select(Process.id).where(
            Process.status == state.QUEUED, Process.deleted_at.is_(None),
            (Process.enqueued_at.is_(None) & (Process.created_at < older_than)) | (Process.enqueued_at < older_than)
        ).limit(limit)
        return list(self.db.execute(q).scalars())

    def bulk_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        return set(self.db.execute(select(Process.id).where(Process.id.in_(ids), Process.batch_id.is_not(None))).scalars())

    def stale_processing(self, older_than: datetime, limit: int) -> list[Process]:
        return list(self.db.execute(select(Process).where(
            Process.status == state.PROCESSING, Process.started_at < older_than).limit(limit)).scalars())

    def expired(self, now: datetime, limit: int) -> list[Process]:
        return list(self.db.execute(select(Process).where(
            Process.expires_at < now, Process.deleted_at.is_(None),
            Process.status.in_(state.TERMINAL)).limit(limit)).scalars())
