from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JsonType, TimestampMixin


class Plan(Base, TimestampMixin):
    """All tunable business limits. Nothing here is hard-coded in application code."""
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # free | pro | enterprise
    requests_per_minute: Mapped[int] = mapped_column(Integer)       # per API key
    tenant_requests_per_minute: Mapped[int] = mapped_column(Integer)  # across all keys of a tenant
    upload_requests_per_minute: Mapped[int] = mapped_column(Integer)  # endpoint-specific (POST bills*)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer)       # queued + processing
    daily_quota: Mapped[int] = mapped_column(Integer)               # documents / UTC day
    monthly_quota: Mapped[int] = mapped_column(Integer)
    max_file_bytes: Mapped[int] = mapped_column(BigInteger)
    max_batch_size: Mapped[int] = mapped_column(Integer)
    max_batch_bytes: Mapped[int] = mapped_column(BigInteger)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)
    bulk_requests_per_minute: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    documents_per_minute: Mapped[int] = mapped_column(Integer, default=600, server_default="600")
    max_pages_per_document: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    # Fair share: max jobs of ONE tenant running on workers at once (<= max_concurrent_jobs which bounds backlog).
    max_running_jobs: Mapped[int] = mapped_column(Integer, default=4, server_default="4")


class Client(Base, TimestampMixin):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | suspended
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    # Explicit contractual consent for using corrections to improve models.
    training_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)   # legacy mirror: True iff training_policy == "pooled"
    # none        : corrections are never used to build datasets (default)
    # tenant_only : datasets containing this tenant's examples may only be built FOR that tenant (private model)
    # pooled      : examples may join cross-tenant datasets (explicit contractual consent; set by an operator only)
    training_policy: Mapped[str] = mapped_column(String(16), default="none", server_default="none")
    # Separate from training consent: may document content be sent to an approved EXTERNAL provider? Default no.
    external_processing: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    training_policy_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    training_policy_updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # overrides plan
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    plan: Mapped[Plan] = relationship(lazy="joined")


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_client_id", "client_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)   # public lookup handle, not secret
    key_hash: Mapped[str] = mapped_column(String(64))              # HMAC-SHA256(pepper, full key)
    scopes: Mapped[list] = mapped_column(JsonType, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | revoked
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from_id: Mapped[str | None] = mapped_column(ForeignKey("api_keys.id"), nullable=True)

    client: Mapped[Client] = relationship(lazy="joined")
