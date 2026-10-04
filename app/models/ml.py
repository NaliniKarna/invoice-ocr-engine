from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, event, func, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, TimestampMixin


class ModelVersion(Base, TimestampMixin):
    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("kind", "name", "version", name="uq_model_versions_kind_name_version"),
        # At most one ACTIVE model per kind: promotion is a controlled, atomic swap.
        Index("uq_model_versions_one_active", "kind", unique=True,
              postgresql_where=text("status = 'production'"), sqlite_where=text("status = 'production'")),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))        # ocr | extractor
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(64))
    # candidate -> evaluated -> approved -> production -> retired ; or rejected (see app/ml/registry.py)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("training_datasets.id"), nullable=True)
    metrics: Mapped[dict] = mapped_column(JsonType, default=dict)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(16), nullable=True)   # extraction schema it emits
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature: Mapped[str | None] = mapped_column(String(128), nullable=True)       # HMAC-SHA256 over the artifact digest
    config: Mapped[dict] = mapped_column(JsonType, default=dict)
    training_meta: Mapped[dict] = mapped_column(JsonType, default=dict)   # seed, code/library versions, hyperparameters
    status_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)


class TrainingDataset(Base, TimestampMixin):
    __tablename__ = "training_datasets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_uri: Mapped[str] = mapped_column(String(512))
    record_count: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    quality_report: Mapped[dict] = mapped_column(JsonType, default=dict)
    filters: Mapped[dict] = mapped_column(JsonType, default=dict)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="training", server_default="training")   # training | evaluation
    # tenant:<client_id> | pooled  -- never mixed: a tenant dataset holds only that tenant's examples
    scope: Mapped[str] = mapped_column(String(80), default="pooled", server_default="pooled")
    status: Mapped[str] = mapped_column(String(24), default="active", server_default="active")  # active | flagged_withdrawal | superseded
    schema_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    split_spec: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


class TrainingExample(Base, TimestampMixin):
    """An approved-or-candidate labelled example. Distinct from feedback (raw correction) and from the machine result
    (immutable, stays in ocr_results). `truth` is the reviewed ground truth; it is never written back to a result."""
    __tablename__ = "training_examples"
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "process_id"], ["processes.client_id", "processes.id"],
                             ondelete="CASCADE", name="fk_training_examples_tenant_process"),
        UniqueConstraint("client_id", "process_id", name="uq_training_examples_process"),
        Index("ix_training_examples_client_status", "client_id", "status"),
        Index("ix_training_examples_group", "group_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(64))
    process_id: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(16))
    truth: Mapped[dict] = mapped_column(JsonType)
    source: Mapped[str] = mapped_column(String(24), default="client_correction")   # client_correction | reviewer | import
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | needs_review | approved | rejected
    revision: Mapped[int] = mapped_column(Integer, default=1)
    quality_report: Mapped[dict] = mapped_column(JsonType, default=dict)
    machine_result_sha256: Mapped[str] = mapped_column(String(64))      # which immutable machine output this corrects
    content_sha256: Mapped[str] = mapped_column(String(64))             # hash of `truth`, for duplicate detection
    group_key: Mapped[str] = mapped_column(String(64))                  # vendor/template family: never split across train/eval
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    annotator: Mapped[str | None] = mapped_column(String(100), nullable=True)       # api key id or reviewer id
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_at_creation: Mapped[str] = mapped_column(String(16), default="none")
    ocr_model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)
    extractor_model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)


class DatasetExample(Base):
    """Dataset membership. Survives example deletion as a tombstone (example_id NULL) so a dataset can be flagged."""
    __tablename__ = "dataset_examples"
    __table_args__ = (Index("ix_dataset_examples_dataset", "dataset_id"), Index("ix_dataset_examples_example", "example_id"))

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("training_datasets.id", ondelete="CASCADE"))
    example_id: Mapped[str | None] = mapped_column(ForeignKey("training_examples.id", ondelete="SET NULL"), nullable=True)
    split: Mapped[str] = mapped_column(String(8))   # train | val | test
    group_key: Mapped[str] = mapped_column(String(64))


class EvaluationRun(Base):
    """Immutable history. Application code only INSERTs; an ORM guard and (on PostgreSQL) a trigger reject UPDATE/DELETE."""
    __tablename__ = "evaluation_runs"
    __table_args__ = (Index("ix_evaluation_runs_model", "model_version_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("training_datasets.id"), nullable=True)
    dataset_ref: Mapped[str] = mapped_column(String(200))        # e.g. "synthetic:seed=7:n=60" for non-registered sets
    dataset_sha256: Mapped[str] = mapped_column(String(64))
    split: Mapped[str] = mapped_column(String(8), default="test")
    sample_count: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JsonType)
    slices: Mapped[dict] = mapped_column(JsonType, default=dict)
    config: Mapped[dict] = mapped_column(JsonType, default=dict)
    environment: Mapped[dict] = mapped_column(JsonType, default=dict)
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    result_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)



@event.listens_for(EvaluationRun, "before_update")
@event.listens_for(EvaluationRun, "before_delete")
def _evaluation_runs_are_immutable(mapper, connection, target):  # noqa: ARG001
    raise RuntimeError("evaluation_runs are immutable")
