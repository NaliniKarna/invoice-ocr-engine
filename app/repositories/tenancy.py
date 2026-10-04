from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import ApiKey, Client, Plan


class ClientRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, client_id: str) -> Client | None:
        return self.db.execute(select(Client).where(Client.id == client_id, Client.deleted_at.is_(None))).scalar_one_or_none()

    def lock(self, client_id: str) -> None:
        """Serialise quota-sensitive submissions per tenant (row lock; no-op on SQLite)."""
        self.db.execute(select(Client.id).where(Client.id == client_id).with_for_update())

    def get_plan(self, plan_id: str) -> Plan | None:
        return self.db.get(Plan, plan_id)


class ApiKeyRepository:
    def __init__(self, db: Session):
        self.db = db

    def by_prefix(self, prefix: str) -> ApiKey | None:
        return self.db.execute(select(ApiKey).where(ApiKey.prefix == prefix)).scalar_one_or_none()

    def get_for_client(self, client_id: str, key_id: str) -> ApiKey | None:
        return self.db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.client_id == client_id)).scalar_one_or_none()

    def get(self, key_id: str) -> ApiKey | None:
        return self.db.get(ApiKey, key_id)

    def touch(self, key_id: str, now: datetime) -> None:
        self.db.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=now))
