from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, TimestampMixin


class Feedback(Base, TimestampMixin):
    """One validated field correction. The original result is never mutated."""
    __tablename__ = "feedback"
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "process_id"], ["processes.client_id", "processes.id"],
                             ondelete="CASCADE", name="fk_feedback_tenant_process"),
        Index("ix_feedback_client_process", "client_id", "process_id"),
        Index("ix_feedback_status", "status"),
        Index("ix_feedback_submission", "submission_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    process_id: Mapped[str] = mapped_column(String(64))
    field_path: Mapped[str] = mapped_column(String(64))
    original_value: Mapped[object | None] = mapped_column(JsonType, nullable=True)   # server-side copy
    corrected_value: Mapped[object | None] = mapped_column(JsonType, nullable=True)
    ocr_model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    extractor_model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="validated")  # validated | exported | quarantined
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("training_datasets.id"), nullable=True)


class Review(Base, TimestampMixin):
    """A person looked at the whole document. Corrections, if any, are Feedback rows with the same submission_id; an
    empty set means "confirmed correct as extracted". At most one review per document. The original result is never mutated."""
    __tablename__ = "reviews"
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "process_id"], ["processes.client_id", "processes.id"],
                             ondelete="CASCADE", name="fk_reviews_tenant_process"),
        UniqueConstraint("client_id", "process_id", name="uq_reviews_client_process"),
        Index("ix_reviews_client_created", "client_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    submission_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    process_id: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))            # confirmed | corrected
    api_key_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
