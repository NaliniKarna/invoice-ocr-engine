# Bill OCR API

Multi-tenant OCR-as-a-Service backend: FastAPI · PostgreSQL · Redis · Celery · S3/MinIO.
Contributor review UI: [`docs/WEB_UI.md`](docs/WEB_UI.md). Design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (requirements, decisions, ERD, API, security model, limits, failure modes).

## Implementation status (honest)

| Area | Status |
|---|---|
| API-key auth (HMAC, scopes, revoke/rotate/expiry), tenant isolation, admin API | implemented, tested |
| Upload validation, single + bulk intake, idempotency, quotas, concurrency, distributed rate limits, backpressure | implemented, tested (incl. races on real PostgreSQL) |
| Process state machine, Celery worker, retries, reaper, retention/erasure | implemented, tested; Celery wiring proven in one manual e2e run |
| OCR abstraction (page-level results and failures, word boxes) + Tesseract adapter + optional RapidOCR adapter | implemented; benchmarked on a **synthetic** set only ([`docs/BENCHMARK.md`](docs/BENCHMARK.md)); no real invoices |
| Field extraction | regex **reference baseline** (unchanged behaviour) and a rule-based **layout-aware** extractor; neither is an ML model; accuracy on real invoices **unknown** |
| Canonical schema v2 (additive), normalisation with provenance, Gregorian + Bikram Sambat dates, validation with configurable tolerances, field evidence (internal) | implemented, unit and integration tested |
| Confidence | heuristic (`heuristic-v2`), **uncalibrated**; isotonic calibration code exists but needs >= 500 labelled documents, none exist |
| Feedback ingestion + validation + storage | implemented, tested |
| Consent policy, training examples with quality gates and human review, leak-free dataset snapshots, erasure interaction | implemented, tested ([`docs/ML_PIPELINE.md`](docs/ML_PIPELINE.md)) |
| Evaluation harness (CER/WER, field/financial/date/line-item metrics, paired-bootstrap comparison), immutable evaluation history | implemented, tested |
| Model registry: lifecycle, configurable promotion gates, regression check against production, rollback A-B-A, reference-extractor fallback, signed-artifact loading once per worker | implemented, tested |
| Model training | **wrapper + readiness checks only; no trainer ships** (no labelled data to justify one) |
| Platform hardening (v0.2): error contract, sandboxed upload/render, structural PDF checks, fencing tokens, fair bulk queue, per-plan limits, worker metrics, correlation IDs, optional tracing | implemented, tested (see "Tests") |
| Docker images / compose | written; `docker compose config` validates (static hardening flags present). **Images never built or scanned: UNVERIFIED** (a Docker daemon starts in the audit sandbox, but every registry pull, including the `python:3.11-slim` base, is denied by egress policy) |
| Prometheus: API + worker metrics | implemented; worker endpoint verified against a real Celery worker on :9100 |
| OpenTelemetry tracing | implemented, **off by default**; propagation tested with an in-memory exporter. Real collector: **UNVERIFIED** |
| Dependency scan (pip-audit), SAST (bandit), secret scan (detect-secrets) | run on 2026-10-01; see [`docs/SECURITY_SCANS.md`](docs/SECURITY_SCANS.md). gitleaks and Trivy **not run** (not installable here) |
| Load test | Locust run once on a 2-CPU shared sandbox with trivial near-blank images: shows the stack stays correct under load (0 errors, all jobs completed), **not a capacity measurement** ([`docs/CAPACITY.md`](docs/CAPACITY.md)) |
| RLS, webhooks, pre-signed uploads, container image scan, secret-manager integration | not done |

**Release status (audit of 2026-10-01): NOT production-ready.** No open code defect of blocker class was found (isolation, authorization, artifact loading and recovery were attacked and held), but production readiness is not evidenced: images are unbuilt and unscanned, there is no capacity measurement, the OpenTelemetry collector path is unverified, the document sandbox is not a kernel sandbox, and **there is no real labelled data, so extraction accuracy on real invoices is unmeasured**. Confidence is uncalibrated. Devanagari OCR is unsupported. Full evidence and classification: [`docs/RELEASE_AUDIT.md`](docs/RELEASE_AUDIT.md).

Vocabulary used in the docs: **implemented** (code exists), **tested** (automated tests pass), **benchmarked** (measured on synthetic data only unless stated), **verified** (observed working in a real environment), **unverified** (not observed), **deferred** (consciously not built).

## Directory tree
```
app/
  api/            deps.py (auth, limits) · middleware.py · health.py · v1/bills.py · internal/admin.py
  core/           config · container (composition root) · errors · ids · time · context
  db/             base (naming, JSONB variant) · session
  models/         tenancy · processing · feedback · ml · audit
  schemas/        bills (API models) · common (metadata parsing)
  repositories/   process (tenant-scoped) · tenancy · model_versions
  services/       ingestion · processes · feedback · admin · audit · state · principal
  security/       api_keys · file_validation · ratelimit
  storage/        base · s3 · local · factory
  queue/          base (JobQueue + Celery adapter)
  ocr/            base (OCRProvider, pages) · providers (Tesseract, RapidOCR) · preprocess · preprocess_ops
  layout/         base (Layout, Table, LayoutAnalyzer) · heuristic
  extraction/     base · rules (reference) · layout_rules · normalization · normalize · postprocess · validation · pipeline · schema
  feedback/       validation
  ml/             registry · examples · quality · splits · dataset · evaluation · metrics · calibration · artifacts · runtime · training · external · interfaces · cli
  workers/        celery_app · tasks · job · maintenance
  observability/  logging · metrics
migrations/       Alembic (0001 initial schema + seeded plans · 0002 hardening · 0003 result metadata · 0004 ML pipeline)
benchmarks/       synthetic.py (seeded SYNTHETIC invoices) · run.py (OCR + extraction benchmark) · resources.py · results/ (immutable JSON)
scripts/          bootstrap_client.py · e2e_smoke.py
tests/            unit/ · integration/ · fakes.py · conftest.py
Dockerfile · docker-compose.yml · .env.example · Makefile
```

## Database schema (tables)
`plans` · `clients` · `api_keys` · `batches` · `processes` · `ocr_results` · `idempotency_keys` · `feedback` · `model_versions` · `training_datasets` · `training_examples` · `dataset_examples` · `evaluation_runs` · `audit_logs` — columns, constraints and ERD in the architecture doc; source of truth is `app/models/` and the migration.

## Run locally

**A. Docker (untested here — may need small fixes):**
```bash
cp .env.example .env        # then replace the CHANGE_ME secrets (python -c "import secrets; print(secrets.token_urlsafe(48))")
docker compose up --build -d
docker compose run --rm api python -m scripts.bootstrap_client "Acme" --plan pro   # prints the API key once
curl -H "X-API-Key: $KEY" -F file=@invoice.png http://localhost:8000/api/v1/bills
```
**B. Without Docker (how it was verified):** Postgres + Redis + tesseract installed locally
```bash
pip install -r requirements-dev.txt pytesseract
export OCR_DATABASE_URL=postgresql+psycopg://ocr:ocr@localhost:5432/ocr OCR_REDIS_URL=redis://localhost:6379/0 \
       OCR_API_KEY_PEPPER=$(python -c "import secrets;print(secrets.token_urlsafe(48))") \
       OCR_ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(48))") \
       OCR_STORAGE_BACKEND=local OCR_OCR_PROVIDER=tesseract
alembic upgrade head
uvicorn app.main:app_factory --factory --port 8000 &
celery -A app.workers.celery_app worker -Q ocr --concurrency=2 &
PROMETHEUS_MULTIPROC_DIR=/tmp/prom OCR_WORKER_METRICS_PORT=9101 celery -A app.workers.celery_app worker -Q ocr_bulk --concurrency=1 -n bulk@%h &   # bulk capacity is separate
celery -A app.workers.celery_app beat &          # exactly one
python scripts/e2e_smoke.py http://localhost:8000 "$OCR_ADMIN_TOKEN"
```
**Tests**
```bash
pytest                                   # SQLite + fakeredis (8 PostgreSQL-only tests skip)
OCR_TEST_DATABASE_URL=postgresql+psycopg://ocr:ocr@localhost:5432/ocr_test \
OCR_TEST_REDIS_URL=redis://localhost:6379/15 pytest     # real PG via Alembic + real Redis Lua (DESTRUCTIVE to that DB/Redis db)
```
Set `OCR_API_KEY_PEPPER`/`OCR_ADMIN_TOKEN` are defaulted by `tests/conftest.py`.

## Tests
Measured on 2026-10-01 after the audit changes: **457 pass** with real PostgreSQL 16 + Redis (line coverage of `app/`: 90%, 5406 statements, 567 missed, via `pytest --cov=app`); **449 pass, 8 skip** on the default SQLite + fakeredis run (the 8 skips are PostgreSQL-only tests). Subprocess children (`render_child`, `validate_child`), Celery entry modules, the OCR provider adapters and the CLI are not measured by line coverage; the Tesseract adapter is exercised by two real-engine tests and by the benchmark.
History: 198 baseline tests (before v0.3) + 189 added in v0.3 = 387; the audit added 70 (`tests/integration/test_final_audit.py`) = 457. The 198 baseline tests still pass. Changed existing tests, each for a stated reason: `test_openapi_documents_real_surface_only` (additive contract: now requires every original path plus exactly the 11 new operator ML paths), `test_every_api_route_requires_authentication` (strengthened: it was iterating zero routes under FastAPI 0.142, see the audit), the pipeline-versions check in `test_hardening.py` (subset check because schema version is now "2"), the raw-feedback dataset test (rewritten, stricter), and one flaky ML split test (made deterministic). No security or isolation assertion was weakened or removed.
Concurrent pytest runs against the same PostgreSQL database interfere with each other (the fixture rebuilds the schema); run one at a time per database.
unit: API keys, file validation, structural PDF/image checks and sandbox limits, limiter, state machine, storage keys, extraction, feedback rules ·
integration: auth/scopes/rotation/revocation, upload + worker flow, cancel/delete/retention, **tenant isolation incl. 404 equality across verbs**, idempotency (incl. bulk, cross-tenant), bulk partial failure + backpressure + fairness, quotas/concurrency/rate limits, worker retry/fencing/duplicate delivery, reaper, orphan sweep, audit retention, feedback, dataset export, model registry, S3 (moto), error contract, DB/storage failure behaviour, correlation IDs, log redaction, metrics cardinality, health, settings validation, tracing propagation, and PostgreSQL-only races (concurrency limit, daily quota, feedback, migration 0001 to head and back, index usage).

## Known limitations
* Extraction is rule-based; English/Latin script only. **Devanagari OCR is unsupported**: the default engine is English Tesseract. If an engine emits Devanagari characters the result gets the `unsupported_script` error issue and is forced to review. An English-only engine emits Latin-looking noise instead, which cannot be detected as a script; on one synthetic Nepali invoice this produced wrong values with confidence 0.13 and `requires_review=true` (n=1, not a guarantee). Devanagari digits are normalised. Bikram Sambat dates are converted with `nepali-datetime` (1970-2099). **No accuracy claim is made for real invoices**: the only measurements are on synthetic data generated from templates the extractors were developed against (see `docs/BENCHMARK.md`). Confidence is uncalibrated and flagged `confidence_calibrated: false`.
* Removing a tenant's data from an already-trained model is not automated: datasets are flagged and must be rebuilt and the model retrained.
* PDF/image parsing happens in rlimit-bounded child processes with an empty environment and a structural check (hex-escaped names, compressed object streams, bounded decode, XObject dimensions, object count). This is resource and active-content hardening, not malware scanning, and it is not a kernel sandbox (no seccomp or namespaces); container isolation is still required.
* Quota/concurrency use the DB under a tenant lock: exact, but serialises one tenant's submissions.
* Replayed idempotent responses are snapshots (status shown is the original `queued`).
* A worker killed mid-job leaves the process `processing` until the reaper threshold (default 900s) passes; recovery was verified on a real stack, but latency is bounded below by that threshold.
* Audit-log purge scans by `created_at` without a dedicated index; acceptable at modest volume, add an index before the table is large.
* Index-usage test disables seq scans to prove a usable index exists; it does not prove planner choice at production volume.
* Single-region, no DR story, no representative load or soak testing, no container image scan, Docker images never built.
* Redis holds the job queue: without persistence a Redis restart loses queued messages. Rows stay `queued` in PostgreSQL and the reaper re-enqueues them after `stale_queued_seconds` (default 600 s); verified on a real stack, but it means up to that delay. Run Redis with persistence and `noeviction`.
* One shared operator token protects all `/internal` endpoints: there is no per-operator identity, and `by`/`reviewer` fields are free text. Keep the admin listener private.
* Model and dataset artifacts are read fully into worker memory (artifact cap 512 MB). Only operators can register them.

## Remaining TODOs (priority order)
1. Build and scan images in CI (Docker was unavailable): Trivy/Grype, SBOM; run `docker compose up` end to end.
2. Run the Locust scenarios on a production-like stack and record p95/p99, queue age and saturation points.
3. Collect a consented, reviewed, real labelled set (hundreds of documents per template family), re-run `benchmarks`/evaluation on it, then decide on a learned layout model; fit and approve a calibrator.
4. Real OpenTelemetry collector verification, dashboards and alerts (queue age, failure rate, 429 rate).
5. Webhooks, list endpoint, corrected-result view, pre-signed direct upload.
6. A `Trainer` implementation (only once data supports it), shadow/canary evaluation of a candidate against live traffic.
7. Postgres RLS, partitioning, PgBouncer, secret-manager integration, pepper rotation.
8. Kernel-level worker sandboxing (seccomp/gVisor) if untrusted PDFs at scale.
