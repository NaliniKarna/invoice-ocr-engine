from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.models import ModelVersion


class ModelVersionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, mv_id: str) -> ModelVersion | None:
        return self.db.get(ModelVersion, mv_id)

    def find(self, kind: str, name: str, version: str) -> ModelVersion | None:
        return self.db.execute(select(ModelVersion).where(
            ModelVersion.kind == kind, ModelVersion.name == name, ModelVersion.version == version)).scalar_one_or_none()

    def ensure(self, kind: str, name: str, version: str, *, status: str = "candidate") -> ModelVersion:
        """Register a provider's (kind,name,version) the first time a worker uses it."""
        existing = self.find(kind, name, version)
        if existing:
            return existing
        mv = ModelVersion(id=new_id("model"), kind=kind, name=name, version=version, status=status)
        try:
            with self.db.begin_nested():
                self.db.add(mv)
            return mv
        except IntegrityError:
            return self.find(kind, name, version)  # type: ignore[return-value]

    def active(self, kind: str) -> ModelVersion | None:
        return self.db.execute(select(ModelVersion).where(
            ModelVersion.kind == kind, ModelVersion.status == "production")).scalar_one_or_none()
