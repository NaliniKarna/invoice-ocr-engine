from __future__ import annotations

from datetime import datetime

from sqlalchemy import (text, BigInteger, Boolean, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JsonType, TimestampMixin


class Batch(Base, TimestampMixin):
    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("client_id", "id"), Index("ix_batches_client_created", "client_id", "created_at"))

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="RESTRICT"))
    total: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[int] = mapped_column(Integer, default=0)


class Process(Base, TimestampMixin):
    """One document-processing job. State machine lives in app/services/state.py."""
    __tablename__ = "processes"
    __table_args__ = (
        UniqueConstraint("client_id", "id", name="uq_processes_client_id_id"),  # target of composite FKs
        ForeignKeyConstraint(["client_id", "batch_id"], ["batches.client_id", "batches.id"]),
        Index("ix_processes_client_created", "client_id", "created_at"),
        Index("ix_processes_client_status", "client_id", "status"),
        # Reaper scans: tiny partial indexes (only rows in the transient states are indexed).
        Index("ix_processes_queued_enqueued", "enqueued_at", postgresql_where=text("status = 'queued'")),
        Index("ix_processes_processing_started", "started_at", postgresql_where=text("status = 'processing'")),
        Index("ix_processes_client_processing", "client_id", postgresql_where=text("status = 'processing'")),
        Index("ix_processes_batch", "batch_id"),
        Index("ix_processes_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="RESTRICT"))
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str] = mapped_column(String(64))
    file_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)  # null once purged
    client_metadata: Mapped[dict] = mapped_column(JsonType, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)  # safe, generic
    error_retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # correlation: HTTP request -> job
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ocr_model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)
    extractor_model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)

    result: Mapped["OcrResult | None"] = relationship(
        back_populates="process", uselist=False, lazy="select",
        primaryjoin="Process.id == OcrResult.process_id", foreign_keys="OcrResult.process_id")


class OcrResult(Base, TimestampMixin):
    __tablename__ = "ocr_results"
    __table_args__ = (
        ForeignKeyConstraint(["client_id", "process_id"], ["processes.client_id", "processes.id"],
                             ondelete="CASCADE", name="fk_ocr_results_tenant_process"),
        Index("ix_ocr_results_client", "client_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[str] = mapped_column(String(64))  # denormalised on purpose: tenant ownership
    schema_version: Mapped[str] = mapped_column(String(16), default="1")
    preprocessing_version: Mapped[str] = mapped_column(String(64), default="1", server_default="1")
    data: Mapped[dict | None] = mapped_column(JsonType, nullable=True)  # null after purge
    field_confidence: Mapped[dict] = mapped_column(JsonType, default=dict)
    issues: Mapped[list] = mapped_column(JsonType, default=list)
    confidence: Mapped[float] = mapped_column(Float)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_artifact_key: Mapped[str | None] = mapped_column(String(512), nullable=True)  # raw OCR JSON in object storage
    ocr_model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    extractor_model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    ocr_ms: Mapped[int] = mapped_column(Integer, default=0)
    extraction_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_ms: Mapped[int] = mapped_column(Integer, default=0)
    # Internal only (never returned by the public API): field evidence, stage versions, per-page status, fallback info.
    meta: Mapped[dict] = mapped_column(JsonType, default=dict, server_default="{}")

    process: Mapped[Process] = relationship(back_populates="result", foreign_keys=[process_id],
                                            primaryjoin="Process.id == OcrResult.process_id")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("client_id", "key", name="uq_idempotency_client_key"),
                      Index("ix_idempotency_expires", "expires_at"))

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
