"""Central, typed configuration. All business limits live in the `plans` table,
not here; settings hold infrastructure and hard safety ceilings only."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OCR_", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "bill-ocr-api"

    database_url: str = "postgresql+psycopg://ocr:ocr@localhost:5432/ocr"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout_s: int = 10          # fail fast instead of queueing requests behind a starved pool
    db_statement_timeout_ms: int = 30_000
    db_lock_timeout_ms: int = 10_000
    db_idle_in_tx_timeout_ms: int = 60_000
    redis_url: str = "redis://localhost:6379/0"

    # Secrets (no defaults on purpose: the app refuses to boot without them)
    api_key_pepper: SecretStr
    admin_token: SecretStr
    metrics_token: SecretStr | None = None

    # Storage
    storage_backend: Literal["s3", "local"] = "s3"
    s3_endpoint_url: str | None = None  # e.g. http://minio:9000 ; None => AWS
    s3_bucket: str = "bill-ocr"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_sse: str | None = "AES256"  # or "aws:kms"; None disables (dev MinIO without KMS)
    local_storage_dir: str = "./.data/objects"

    # Hard safety ceilings (plans may only lower these)
    max_upload_bytes: int = 10 * 1024 * 1024
    max_request_bytes: int = 120 * 1024 * 1024
    max_pdf_pages: int = 20
    max_image_pixels: int = 40_000_000
    max_metadata_bytes: int = 4096

    # Networking
    cors_allow_origins: list[str] = Field(default_factory=list)  # server-to-server API: empty = CORS off
    trusted_proxy_hops: int = 0  # number of trusted reverse proxies in front (X-Forwarded-For)
    ip_requests_per_minute: int = 600  # pre-auth, per source IP
    auth_failures_per_minute: int = 20  # per source IP
    rate_limit_fail_open: bool = False

    # Idempotency / lifecycle
    idempotency_ttl_hours: int = 24
    job_max_attempts: int = 3
    job_time_limit_seconds: int = 300
    stale_queued_seconds: int = 600
    stale_processing_seconds: int = 900
    queue_interactive: str = "ocr"       # single-document submissions
    queue_bulk: str = "ocr_bulk"         # /bills/bulk submissions (separate so bulk cannot starve interactive)
    queue_max_depth: int = 10_000        # interactive queue; beyond this intake answers 503
    bulk_queue_max_depth: int = 5_000
    worker_defer_seconds: int = 10       # retry delay when a tenant is at its running-jobs cap
    worker_metrics_port: int = 9100
    render_timeout_seconds: int = 60     # sandboxed decode/render wall clock
    render_memory_mb: int = 1536         # RLIMIT_AS for the sandboxed decode/render child
    max_pdf_objects: int = 50_000
    max_pdf_decoded_bytes: int = 64 * 1024 * 1024   # per content/embedded stream, decompression-bomb guard
    otel_enabled: bool = False           # also needs opentelemetry packages + OTEL_EXPORTER_OTLP_* env
    default_retention_days: int = 30
    ocr_auto_orient: bool = False        # Tesseract OSD rotation; off until a benchmark on real data supports it
    audit_retention_days: int = 365      # audit rows older than this are purged in bounded batches

    # OCR / extraction
    ocr_provider: Literal["unconfigured", "tesseract", "rapidocr"] = "unconfigured"
    # Field extractor used when no registry model is in production. The regex baseline stays the safe default for
    # compatibility; the layout-aware rules read headers, bill-to blocks and tables better (see docs/BENCHMARK.md,
    # synthetic data only). The regex baseline is always the fallback if the chosen extractor crashes.
    reference_extractor: Literal["rule-based-baseline", "layout-rules", "layout-kv"] = "rule-based-baseline"
    # Alternate extractors (comma list of the names above) tried on the SAME OCR result when the primary leaves a key field missing
    # or suspicious. The result with fewer errors / key-field problems wins; the primary wins ties. Empty = off.
    extractor_alternates: str = ""
    # Report missing or suspicious vendor / invoice number / date / total (and tax or subtotal implied by the numbers) as validation issues.
    validate_required_fields: bool = True
    ocr_languages: str = "eng"
    tesseract_psm: int = 3   # Tesseract page segmentation mode (3 = engine default; 6 = single block, better for narrow receipts)
    review_confidence_threshold: float = 0.80
    assume_day_first_dates: bool = True
    # Second OCR pass for poor first results (phone photos, small or uneven images). The first pass is unchanged, so clean
    # documents keep their measured behaviour; the retry runs only when the first result has an error-severity issue or low
    # confidence, and its result replaces the first only if strictly better (fewer errors, then higher confidence).
    # Empty ops = no retry. Receipt-style settings hurt clean synthetic invoices (see docs/BENCHMARK.md), hence a retry.
    retry_preprocess_ops: str = ""
    retry_tesseract_psm: int = 6
    retry_below_confidence: float = 0.5
    retry_max_pages: int = 3
    retry_on_key_fields: bool = True       # also retry when a key field is missing or suspicious, even if confidence is above retry_below_confidence
    # Several retry variants, tried in order until the result has no key-field problem: "ops@psm;ops@psm", e.g. "upscale@3;upscale,adaptive@6".
    # A clean small screenshot needs a plain upscale (adaptive thresholding hurts it); a phone photo of a receipt needs upscale+adaptive with psm 6.
    # Empty = one variant from retry_preprocess_ops / retry_tesseract_psm.
    retry_passes: str = ""
    preprocess_ops: str = ""             # comma list from app.ocr.preprocess_ops; empty = none (benchmark before enabling)
    validation_tolerance_abs: float = 0.02
    validation_tolerance_rel: float = 0.0005
    # ML / feedback governance (all values are operator-tunable; none was derived from real data)
    ml_max_examples_per_tenant_per_day: int = 200
    ml_max_tenant_share: float = 0.5           # cap of one tenant's share of a pooled dataset (when >= 3 tenants contribute)
    ml_anomaly_change_fraction: float = 0.6    # share of key fields changed by one submission above which it is flagged
    ml_model_signing_key: SecretStr = SecretStr("")   # HMAC key for model artifacts; empty disables loading of custom models
    ml_external_providers_enabled: bool = False       # deny by default: no document content leaves the deployment
    ml_external_provider_allowlist: str = ""          # comma list; additionally requires tenant policy "external_ok"
    partial_page_policy: Literal["review", "fail"] = "review"   # a failed page forces review, or fails the job

    @model_validator(mode="after")
    def _check_ml(self) -> "Settings":
        from app.ocr.preprocess_ops import parse_ops
        parse_ops(self.preprocess_ops)        # unknown operation => refuse to start, not fail every job
        parse_ops(self.retry_preprocess_ops)
        from app.ocr.providers import parse_retry_passes
        parse_retry_passes(self.retry_passes, self.retry_tesseract_psm)         # malformed => refuse to start
        names = {"rule-based-baseline", "layout-rules", "layout-kv"}
        if any(n.strip() and n.strip() not in names for n in self.extractor_alternates.split(",")):
            raise ValueError(f"OCR_EXTRACTOR_ALTERNATES must be a comma list of {sorted(names)}")
        if self.tesseract_psm not in (3, 4, 6, 11) or self.retry_tesseract_psm not in (3, 4, 6, 11):
            raise ValueError("Tesseract page segmentation mode must be one of 3, 4, 6, 11")
        if not (0 <= self.retry_below_confidence <= 1 and 1 <= self.retry_max_pages <= 20):
            raise ValueError("OCR retry settings out of range")
        if not (0 <= self.validation_tolerance_abs <= 10 and 0 <= self.validation_tolerance_rel <= 0.05):
            raise ValueError("validation tolerances out of the sane range (abs 0..10, rel 0..0.05)")
        if not (0 < self.ml_max_tenant_share <= 1):
            raise ValueError("OCR_ML_MAX_TENANT_SHARE must be in (0, 1]")
        return self

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.stale_processing_seconds < int(self.job_time_limit_seconds * 1.5):
            # The reaper must never requeue a job whose live worker can still legitimately be running.
            raise ValueError("OCR_STALE_PROCESSING_SECONDS must be >= 1.5x OCR_JOB_TIME_LIMIT_SECONDS")
        if self.job_time_limit_seconds < 60:
            raise ValueError("OCR_JOB_TIME_LIMIT_SECONDS must be >= 60")
        return self

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.env == "prod":
            for name in ("api_key_pepper", "admin_token"):
                if len(getattr(self, name).get_secret_value()) < 32:
                    raise ValueError(f"OCR_{name.upper()} must be >= 32 chars in prod")
            if self.storage_backend == "local":
                raise ValueError("local storage backend is not allowed in prod")
            if "*" in self.cors_allow_origins:
                raise ValueError("wildcard CORS is not allowed in prod")
            if ":ocr@" in self.database_url or "@localhost" in self.database_url:
                raise ValueError("OCR_DATABASE_URL uses development credentials/host in prod")
            if self.storage_backend == "s3":
                key = self.s3_access_key.get_secret_value() if self.s3_access_key else ""
                sec = self.s3_secret_key.get_secret_value() if self.s3_secret_key else ""
                if key in ("", "minioadmin") or sec in ("", "minioadmin"):
                    raise ValueError("OCR_S3_ACCESS_KEY/SECRET_KEY missing or default in prod "
                                     "(use an IAM role/instance credentials by setting real keys or extending the factory)")
                if self.s3_sse is None:
                    raise ValueError("OCR_S3_SSE must be set in prod (server-side encryption)")
            if self.metrics_token is None:
                raise ValueError("OCR_METRICS_TOKEN is required in prod")
            if self.ocr_provider == "unconfigured":
                raise ValueError("OCR_OCR_PROVIDER must be configured in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
