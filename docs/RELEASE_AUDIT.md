# Release audit (v0.3.0, 2026-10-01)

**Status: NOT production-ready.**

The application code and tests are strong, and attacks on isolation, authorization, model loading and failure recovery held. That is not evidence of production readiness. Container images were never built or scanned, no real-document accuracy exists, no capacity measurement exists, the OpenTelemetry collector path was never exercised, and the document sandbox is a resource-bounded child process, not a kernel sandbox. Each of these is listed below with its classification.

Vocabulary: **implemented** (code exists), **tested** (automated tests pass), **benchmarked** (synthetic data only unless stated), **verified** (observed working in a real environment), **unverified** (not observed), **deferred** (not built).

Environment of this audit: one Linux sandbox, 2 CPUs, 8 GiB, Python 3.11.15, PostgreSQL 16, Redis, Tesseract 5.3.4, no GPU. "Real stack" below means real uvicorn, Celery workers (interactive and bulk queues), beat, PostgreSQL, Redis and Tesseract as separate processes, with local-filesystem object storage (S3/MinIO was not part of the real-stack runs; S3 behaviour is covered by moto tests only).

## 1. Regression baseline (verified, not trusted)

| Claim in the Prompt 3 report | Measured at the start of this audit | Result |
|---|---|---|
| PostgreSQL 16 + Redis: 387 passed | 386 passed, **1 failed** (`test_training_and_evaluation_sets_do_not_share_groups`) | **Not reproduced.** The test was flaky (see finding F4) |
| SQLite: 379 passed, 8 skipped | 379 passed, 8 skipped | Reproduced |
| 89% line coverage of `app/` | 89% (5360 statements, 606 missed) | Reproduced |
| 198 original tests pass, 189 added | all 198 baseline tests passed; 189 added | Reproduced (387 collected) |

## 2. Test results after the audit

Final run after all changes (see section 11 for the commands):

| Run | Result |
|---|---|
| PostgreSQL 16 + Redis, full suite | **457 passed**, 0 failed (145.5 s) |
| SQLite + fakeredis, full suite | **449 passed, 8 skipped** (the 8 are PostgreSQL-only tests; 68.3 s) |
| Line coverage of `app/` (PostgreSQL run) | **90%** (5406 statements, 567 missed). Not measured by line coverage: subprocess children, Celery entry modules, CLI; the OCR adapters are only partly covered |

Tests by area (457 collected; counted by file, every one passing in the PostgreSQL run):

* Security: 132 (auth 12, tenant isolation 9, API keys 5, content checks and sandbox 16, file validation 15, rate limiter 5, release-audit hostile tests 70).
* ML / OCR / extraction: 220 (normalisation 55, ML units 34, ML runtime and operator API 33, worker + feedback + ML 19, validation v2 16, ML lifecycle 16, pipeline 15, preprocessing 8, multi-page 6, extraction 6, feedback validation 6, schema compatibility 5, migration 0004 1).
* Integration, failure and platform: 105 (platform hardening 31 and 24, idempotency/bulk/limits 20, upload and processing 15, state machine and storage 11, PostgreSQL concurrency 3, S3 1).

The audit added 70 tests in `tests/integration/test_final_audit.py` (68 first, plus two real-Tesseract tests; they are skipped if the Tesseract binary is absent). No existing test was deleted. Existing tests that changed, with the reason:
1. `test_openapi_documents_real_surface_only` (Prompt 3, additive contract): still requires every original path, now also exactly the 11 operator ML paths.
2. `test_every_api_route_requires_authentication`: **strengthened** (finding F1).
3. `test_training_and_evaluation_sets_do_not_share_groups`: made deterministic (finding F4); the assertion on leakage is unchanged.
4. Earlier, in Prompt 3: the pipeline-versions subset check and the rewritten raw-feedback dataset test. Both are documented in the README.

## 3. Findings

### Fixed in this audit

| Id | Class | Finding | Fix | Evidence |
|---|---|---|---|---|
| F1 | WARNING (test quality) | `test_every_api_route_requires_authentication` iterated `app.routes`. With FastAPI 0.142 included routers are lazy objects with empty paths, so the loop examined **zero routes** and could never fail. The authorization guarantee was untested by that test. | The test now takes every operation from the OpenAPI document and requires `401` without credentials. A second test pins the surface (6 admin + 12 ML operations, 7 public) so an unaudited route fails the build. | `test_final_audit.py`, `test_hardening.py` |
| F2 | WARNING | Guessing the operator token was not throttled (the failed-auth limiter covered tenant keys only). | `admin_auth` now shares the per-IP failed-auth budget (429 + `Retry-After`). | `test_operator_token_guessing_is_throttled` |
| F3 | WARNING (defence in depth) | A registry row's `artifact_uri` was read from storage **before** it was checked, and was not restricted to a location. Operator-only, and the signature still had to verify, but a row could point a worker at any object. | `check_uri` now requires the `models/` prefix and the traversal-proof key rules, and runs before any read. | 9 traversal and location cases; asserts storage is never read |
| F4 | WARNING (test reliability) | The leakage test used a fixed split seed with random tenant ids; about 1 run in 7 had no test-split group and failed. | Seed chosen per run so both splits exist; assertions unchanged. | 12 consecutive full ML-suite runs, 0 failures afterwards |
| F5 | WARNING (governance) | After consent withdrawal, datasets were flagged but trained **models** were not identifiable through the API. | `GET /internal/v1/models` returns `dataset_id`, `parent_id`, `affected_by_withdrawal`, `flagged_datasets` (own dataset or any ancestor). Retraining is still manual. | `test_models_trained_on_withdrawn_data_are_identified_for_manual_retraining` |
| F6 | WARNING (unsupported input) | Devanagari characters in OCR output were not flagged. | Error issue `unsupported_script` and forced review. See the limit in section 6. | 3 tests |
| F7 | Hygiene | New bandit B101 (`assert`) in `app/ocr/base.py`. | Replaced by an explicit condition. | bandit |
| F8 | Documentation | `docs/SECURITY_SCANS.md` reported detect-secrets "0 findings"; the tool scans nothing in a non-git tree by default. | Re-ran with `--all-files`: 14 candidates, all false positives. File corrected. | section 8 |
| F9 | Coverage gap | The real Tesseract adapter was exercised only by benchmarks and a manual run. | Two real-engine tests (total, subtotal, tax and invoice number exact on a typeset invoice; blank page forces review). | `test_real_tesseract_*` |

### Open, by class

**BLOCKER** (code defect that must be fixed before production): none found. No tenant-isolation failure, authorization bypass, data leak, unsafe model load, unbounded document processing or corrupted financial result was found in the checks listed in section 4. This is a statement about the checks made, not a proof of absence.

**WARNING** (environment or deployment dependent; each stops a production-ready claim):
* W1. Docker images were never built, so the Dockerfile, the compose stack, the health checks inside containers and the read-only root filesystem are **unverified**. A Docker daemon starts in the sandbox, but every registry (Docker Hub, `mirror.gcr.io`, `public.ecr.aws`, `quay.io`, `ghcr.io`) answered 403 from the egress policy, so the `python:3.11-slim` base could not be pulled. `docker compose config` validates the file and shows `read_only`, `cap_drop`, `no-new-privileges` and health checks present (static check only).
* W2. No container vulnerability scan (Trivy or equivalent): no image, and the Trivy binary could not be downloaded.
* W3. No capacity measurement. The one Locust run (2-CPU shared sandbox, near-blank test images) is correctness under load only. See `CAPACITY.md`.
* W4. OpenTelemetry against a real collector is unverified; tracing is off by default.
* W5. The upload/render sandbox is a rlimit-bounded child process with an empty environment, a timeout and a process-group kill. It is not a kernel sandbox (no seccomp, namespaces, gVisor) and not a malware scanner. Untrusted PDFs at scale need container isolation or stronger.
* W6. Redis is the job queue. Without persistence a restart loses queued messages; the reaper recovers them only after `stale_queued_seconds` (default 600 s). Run Redis with persistence, `noeviction` and HA.
* W7. One shared operator token, no per-operator identity: the audit trail records the actor as `admin`, and `by`/`reviewer` are free text. The admin listener must be on a private network.
* W8. Model and dataset artifacts are read fully into worker memory (cap 512 MB for models). Operator-controlled, but a very large artifact could exhaust a worker.
* W9. gitleaks was not run (not installable here); detect-secrets was.
* W10. S3/MinIO was exercised only through moto, not through the real-stack runs.

**KNOWN LIMITATION** (accepted and documented):
* No real labelled dataset; accuracy on real invoices is **unmeasured**. All benchmark numbers are synthetic (72 documents, seed 7) and must not be quoted as production accuracy.
* Confidence is heuristic and uncalibrated (`heuristic-v2`, `confidence_calibrated=false`). No calibrator exists.
* No Devanagari OCR. No custom model, no trainer.
* A tenant's influence on an already-trained model is not removed automatically. Affected datasets and models are identifiable (F5); retraining is manual.
* The layout-rule extractor is likely overfit to the synthetic templates it was written against.
* Tolerances for financial validation are engineering judgement, not fitted to data.
* The `language` field is a script-ratio heuristic. `en` means "no Devanagari seen", not that the text was verified as English.

## 4. What was attacked, and the outcome

All items are in automated tests unless marked "real stack".

**Tenant isolation.** Tenant B against tenant A's process (read, delete, cancel, feedback) and batch: the responses are byte-identical to those for a non-existent id except `request_id`. A's data is unchanged afterwards. A's feedback never appears in B's result. The public result contains no storage path, tenant id, artifact, signature, training or example data. Datasets, evaluation runs, models and audit logs have no tenant-facing endpoint at all; every `/internal` operation rejects a tenant key (tested for all 18). The database refuses cross-tenant rows by composite foreign key (every table with `client_id` has a foreign key except `audit_logs`, which is designed to outlive tenants).

**Operator ML endpoints (11 paths, 12 operations).** Each was called with: no credential, empty/wrong/upper-cased/truncated/padded operator token, a tenant key in `X-API-Key`, as the admin token and as a bearer token, the admin token in `X-API-Key`, a revoked tenant key, and keys with narrow scopes. Every case returned 401 with the same error code and without echoing a secret. A positive control (the correct token) is not rejected. Token guessing is throttled after 5 failures per minute per IP.

**API keys.** HMAC-SHA256 with a server pepper, 256-bit secret, shown once, constant-time compare with a dummy compare for unknown prefixes. Revocation, expiry, scopes and rotation grace are covered by Prompt 2 tests that still pass. New: a key with a valid prefix and altered secret is indistinguishable from an unknown key; neither tenant keys nor the admin token (including the secret part of a key) appear in captured logs across success, near-miss, failure and a full upload-and-process flow.

**Documents.** Existing and still passing: executable renamed as PNG, MIME/extension mismatch ignored, empty and oversized files, truncated images, pixel-count decompression bombs, PDF page caps, PDF JavaScript/Launch/OpenAction/embedded files including hex-escaped names and compressed object streams, encrypted PDFs, Flate bombs, huge image XObjects, object-count limits, sandbox timeout, memory exhaustion and secret non-inheritance, malicious filenames never used in storage paths. No code path executes uploaded content: no `pickle`, `eval`, `exec`, shell, or YAML load in `app/`; Celery uses JSON only. This is resource bounding and structural rejection, not malware scanning and not a kernel sandbox.

**Model loading.** Signature (HMAC over the SHA-256) and checksum are verified; tampered, unsigned, wrong-key and other-deployment-key artifacts are refused; pickle magic bytes are refused even with an allowed extension; oversize artifacts are refused; an unpromoted signed candidate is never served; every bad case falls back to the reference extractor with a metric and a sticky error (no retry storm). New in this audit: locations outside `models/`, traversal, absolute paths, backslashes, double slashes and another tenant's object key are refused before any read. Promotion needs gates and cannot skip states; tenant keys cannot call it (all tested).

**Workers and recovery (real stack).**
* SIGKILL: a worker process group was stopped and killed while a job was `processing` (attempt 1). After restart and one reaper pass (stale threshold shortened to 90 s for the test) the job returned to `queued` with `worker_lost`, ran as attempt 2 and completed. One result row.
* Redis loss: Redis was killed with 5 jobs queued. Uploads answered 503, `/readyz` 503, `/healthz` 200. After restart the 5 rows were still `queued` with an empty broker queue; the reaper re-enqueued them and all 5 completed.
* PostgreSQL loss: reads and uploads answered `503 database_unavailable` with `Retry-After: 5`, `/readyz` 503; after restart the API recovered without a restart; no stack traces in the API log.
* After all runs: 1106 processes, all `completed`, none stuck or failed, 0 processes with more than one result row.
* In tests (Prompt 2, still passing): duplicate delivery, stale-worker complete and fail with fencing tokens, deleted process never processed, bounded attempts, OCR transient and permanent errors, storage failure, DB commit failure, post-commit bookkeeping failure.

**Bulk versus interactive.** Separate queues and separate worker pools. In the Locust run with roughly 28 to 36 bulk messages waiting, interactive documents (489) completed with p95 7.8 s while bulk documents (280) had p95 31.1 s. This is a sandbox-scale observation, not a fairness proof under saturation.

**Database.** Composite foreign keys, partial unique index for one production model per kind, immutable evaluation runs (ORM guard and PostgreSQL trigger), migrations 0001 to 0004 upgrade and downgrade tests, index-usage tests. Unchanged and passing.

## 5. ML and data governance

Verified by tests: machine results are never modified by feedback, example proposal or dataset activity; feedback alone cannot change which model is in production; consent (`none`, `tenant_only`, `pooled`) is enforced at example creation and dataset scope; examples carry provenance and `policy_at_creation`; quality gates flag (not repair) inconsistent totals, contradictory corrections, duplicates, schema mismatches; splits are by tenant-plus-supplier group and never share a group between training and evaluation; evaluation data is excluded from later training sets; promotion needs gates (synthetic evidence never approves); rollback A to B to A works; model and schema versions are stored with each result; anti-poisoning limits (daily per-tenant cap, share cap, anomaly flags, no auto-approval) exist and are heuristics, not a defence against a determined adversary.

Not solved: unlearning. Consent withdrawal erases examples, flags datasets and now identifies affected models; a human must rebuild datasets and retrain.

## 6. OCR and extraction

Pipeline: document, sandboxed render, OCR page by page, layout, extraction, normalisation with provenance, validation with configurable tolerances, heuristic confidence, result with stage versions. Verified on a real stack: a typeset invoice produced exact invoice number, date, subtotal, tax and total and two line items (one document; not an accuracy claim). Failure handling: failed page forces review, extractor crash falls back to the reference extractor and records it, nothing valid fails the job with a safe message. Gregorian and Bikram Sambat dates have explicit tests including invalid, ambiguous and OCR-corrupted dates.

Confidence: the public API describes it as a provisional heuristic and returns `confidence_calibrated=false`. A grep for "calibrated", "production-ready", "highly accurate", "real-world accuracy", "malware", "multilingual", "automatic learning" found no unsupported claim in code or docs after the corrections above.

Devanagari: **unsupported.** The default engine is English Tesseract. When an engine emits Devanagari characters the result gets `unsupported_script` and forced review. An English-only engine emits Latin-looking noise instead, which cannot be seen as a script. Measured once (n=1): a synthetic Nepali invoice produced a garbage vendor, a wrong date and a wrong tax value, with confidence 0.13 for the reference extractor and 0.12 for the layout extractor, `requires_review=true` and four validation issues. That is why the low confidence and validation issues, not script detection, flagged it. It is one sample and not a guarantee.

## 7. Benchmark claim audit

The only benchmark is 72 synthetic documents (seed 7, English, 3 templates, 6 degradations). `docs/BENCHMARK.md` states this first, separates measured from unmeasured, and labels every component. No real-world metric exists or is claimed. Run 1 (before the deskew fix) is kept as history and marked do-not-quote. Custom model training stays deferred: no representative labelled data, no trainer.

## 8. Scans actually run

| Tool | Result |
|---|---|
| pip-audit 2.10.1 on `constraints.txt` | no known vulnerabilities |
| bandit 1.9.4 on `app/` | 0 high, 0 medium, 8 low (reviewed) |
| detect-secrets 1.5.0, `--all-files` | 14 candidates, all false positives |
| gitleaks | **not run** (not installable) |
| Trivy / container scan | **not run** (no image; binary unavailable) |

Details in `SECURITY_SCANS.md`.

## 9. Deployment and observability

| Item | Status |
|---|---|
| Docker image build | UNVERIFIED (registry egress denied) |
| `docker compose up`, container health checks | UNVERIFIED |
| Container vulnerability scan | UNVERIFIED (not run) |
| Real-stack processes (API, interactive worker, bulk worker, beat, PostgreSQL, Redis, Tesseract) | verified, without containers |
| Locust load run | done once, correctness only; no capacity claim |
| Worker Prometheus endpoint | verified on a real Celery worker in an earlier session |
| OpenTelemetry real collector | UNVERIFIED |

## 10. Production conditions (what would change the status)

In priority order:
1. Build the images in CI with network access, run compose, confirm health checks, scan with Trivy and fix what it finds.
2. Collect a consented, reviewed, real labelled set per template family, run the benchmark on a held-out split, and only then decide about auto-acceptance, a calibrator or a model. Until then treat every result as needing review.
3. Run the load test on production-like hardware with representative documents; set worker counts and plan limits from it.
4. Verify the OpenTelemetry path against a real collector; add dashboards and alerts.
5. Run Redis with persistence and HA; keep the admin listener private; add per-operator identity if more than one operator exists.
6. Put the render sandbox inside a kernel-level boundary (gVisor, seccomp, or per-job containers) if untrusted PDFs arrive at scale.
7. Add Devanagari OCR before accepting Nepali-language documents.

## 11. How to reproduce

```
. env (peppers) ; OCR_TEST_DATABASE_URL=postgresql+psycopg://...  OCR_TEST_REDIS_URL=redis://.../15
python -m pytest tests -p no:cacheprovider -o addopts="" -q --cov=app --cov-report=term
unset OCR_TEST_DATABASE_URL OCR_TEST_REDIS_URL; python -m pytest tests -p no:cacheprovider -o addopts="" -q
bandit -q -r app ; detect-secrets scan --all-files --exclude-files 'constraints.txt|benchmarks/results/.*|__pycache__' . ; pip-audit -r constraints.txt --no-deps --disable-pip
locust -f loadtest/locustfile.py --host http://localhost:8000 --headless -u 20 -r 2 -t 180s
```
Run one pytest process per PostgreSQL database; the fixture rebuilds the schema.
