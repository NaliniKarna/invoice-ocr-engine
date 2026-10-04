"""Prometheus metrics. Labels are low-cardinality on purpose (no tenant/process ids)."""
from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP latency", ["method", "route"])
RATE_LIMITED = Counter("rate_limit_rejections_total", "429s", ["scope"])
AUTH_FAILURES = Counter("auth_failures_total", "Failed authentications", ["reason"])
DOCS_SUBMITTED = Counter("documents_submitted_total", "Documents accepted", ["kind"])
DOCS_REJECTED = Counter("documents_rejected_total", "Documents rejected at intake", ["reason"])
JOB_OUTCOMES = Counter("ocr_jobs_total", "Job outcomes", ["outcome"])
JOB_RETRIES = Counter("ocr_job_retries_total", "Job retries")
JOB_DURATION = Histogram("ocr_job_duration_seconds", "Total job duration")
QUEUE_LATENCY = Histogram("ocr_queue_latency_seconds", "Enqueue -> start latency",
                          buckets=(.1, .5, 1, 2, 5, 15, 30, 60, 120, 300, 900))
OCR_DURATION = Histogram("ocr_engine_duration_seconds", "OCR stage", ["provider", "model_version"])
EXTRACT_DURATION = Histogram("extraction_duration_seconds", "Extraction stage", ["extractor", "model_version"])
EXTRACT_FALLBACKS = Counter("extraction_fallbacks_total", "Extractor output rejected, reference extractor used", ["reason"])
EXTRACT_STRATEGY = Counter("extraction_strategy_total", "Alternate extraction strategy run because a key field was missing or suspicious", ["outcome"])   # used_alternate | kept_primary | error
OCR_RETRIES = Counter("ocr_retry_passes_total", "Second OCR pass on a poor first result", ["outcome"])   # used | not_better | error
EXTRACT_FAILURES = Counter("extraction_failures_total", "Extraction stage failures", ["extractor", "reason"])
OCR_PAGE_FAILURES = Counter("ocr_page_failures_total", "Pages that failed OCR", ["provider", "code"])
OCR_PAGES = Histogram("ocr_document_pages", "Pages per document", buckets=(1, 2, 3, 5, 10, 20, 50))
VALIDATION_ISSUES = Counter("extraction_validation_issues_total", "Validation issues raised", ["code", "severity"])
LOW_CONFIDENCE = Counter("extraction_low_confidence_total", "Results below the review threshold")
ML_EXAMPLES = Counter("ml_examples_total", "Training-example lifecycle events", ["outcome"])
ML_DATASETS = Counter("ml_datasets_built_total", "Datasets built", ["kind", "scope_type"])
ML_EVALUATIONS = Counter("ml_evaluation_runs_total", "Evaluation runs recorded", ["kind", "synthetic"])
ML_PROMOTIONS = Counter("ml_promotion_events_total", "Registry lifecycle events", ["event"])
ML_MODEL_LOAD = Histogram("ml_model_load_seconds", "Model artifact load time", ["kind"], buckets=(.01, .05, .1, .5, 1, 5, 15, 60))
ML_MODEL_LOAD_ERRORS = Counter("ml_model_load_errors_total", "Model artifact load failures", ["kind", "reason"])
ML_FALLBACK_ACTIVE = Counter("ml_reference_fallback_total", "Requests served by the reference extractor", ["reason"])
CONFIDENCE = Histogram("extraction_confidence", "Overall confidence",
                       buckets=(.1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0))
REVIEW_REQUIRED = Counter("extraction_requires_review_total", "Results flagged for review")
REAPED = Counter("ocr_jobs_reaped_total", "Stale jobs recovered", ["kind"])

# ---- added in hardening pass ---------------------------------------------------------------
from prometheus_client import Gauge  # noqa: E402

JOBS_RECEIVED = Counter("ocr_jobs_received_total", "Job deliveries seen by workers")
JOBS_DEFERRED = Counter("ocr_jobs_deferred_total", "Jobs put back because the tenant is at its running-jobs cap")
WORKER_ERRORS = Counter("ocr_worker_errors_total", "Worker-side errors by class", ["kind"])
WORKER_ACTIVE_JOBS = Gauge("ocr_worker_active_jobs", "Jobs currently executing", multiprocess_mode="livesum")
WORKER_PROCESSES = Gauge("ocr_worker_processes", "Live worker processes", multiprocess_mode="livesum")
QUOTA_REJECTED = Counter("quota_rejections_total", "Submissions rejected by plan limits", ["reason"])
ORPHANS_SWEPT = Counter("storage_orphans_swept_total", "Orphaned objects deleted")
