# Capacity and overload behaviour

**No capacity figure is claimed.** One Locust run exists (below). It was taken on a shared 2-CPU sandbox, with the API, three Celery processes, PostgreSQL, Redis and the load generator on the same machine, using near-blank 400x300 PNGs that make OCR almost free. It shows the stack behaves correctly under concurrent load. It does not say how many real invoices per second any deployment can process.

## Locust run of 2026-10-01 (Locust 2.46.6, `loadtest/locustfile.py`, 20 users, ramp 2/s, 180 s)
Stack: uvicorn (1 process), Celery `-Q ocr` (concurrency 2), Celery `-Q ocr_bulk` (concurrency 1), beat, PostgreSQL 16, Redis, local object storage, Tesseract 5.3.4. Enterprise plan; the per-IP pre-auth limit was raised to 1,000,000/min because all load came from one IP.

| Request | Count | Failures | p50 | p95 | p99 |
|---|---|---|---|---|---|
| `POST /bills` | 406 | 0 | 330 ms | 550 ms | 680 ms |
| `POST /bills` (idempotent retry, two calls) | 158 | 0 | 330 ms | 520 ms | 690 ms |
| `POST /bills/bulk` (10 files each) | 28 | 0 | 2900 ms | 3500 ms | 3600 ms |
| `GET /bills/{id}` (polling) | 2700 | 0 | 12 ms | 36 ms | 62 ms |
| All | 3292 | 0 | 15 ms | 410 ms | 690 ms |

* Request rate seen by Locust: 18.3 requests/s in total. 769 processes were created and all 769 completed; none failed; no result row was duplicated.
* End-to-end time from creation to completion (from PostgreSQL timestamps): interactive 489 documents, p50 5.7 s, p95 7.8 s, max 9.2 s; bulk 280 documents, p50 24.1 s, p95 31.1 s, max 34.8 s. While the bulk queue held roughly 28 to 36 messages, interactive documents were not slowed to bulk times (separate worker pools).
* Resources: about 876 MB total resident memory for the API and all worker processes together; load average about 5 on 2 CPUs, so the machine was saturated and these latencies include CPU contention with the load generator.
* An earlier run (100 s) with the default per-IP limit of 600 requests/min returned 429 to 531 of 1,480 polling requests from the single load-generator IP. That is the limiter working, and it is why the second run raised the limit. Locust counts a 429 on `GET` as a failure.

Not measured: real invoice images, multi-page PDFs, queue saturation (`503`), sustained soak, a production-like multi-host deployment, PostgreSQL pool exhaustion, any worker count other than 2 + 1.

## Overload behaviour (by design, tested with fakes and small real stacks)
| Condition | Behaviour |
|---|---|
| Interactive queue depth >= `queue_max_depth` (10,000) | New single uploads get `503` with `Retry-After`; nothing is stored |
| Bulk queue depth >= `bulk_queue_max_depth` (5,000) | Bulk accepts what fits; rest is reported per item; fully full gives `503` |
| Tenant at `max_concurrent_jobs` | `429` with `Retry-After` |
| Tenant at `max_running_jobs` | Jobs wait in the queue (deferred, no attempt consumed); other tenants continue |
| Rate or quota exceeded | `429` with `Retry-After` (quota includes reset time) |
| Redis down | Fail closed: uploads `503`, `/readyz` 503, `/healthz` 200 (verified on a real stack, 2026-10-01). Redis without persistence loses queued messages; the rows stay `queued` and the reaper re-enqueued all 5 test jobs after `stale_queued_seconds`; all completed |
| DB down | `503 database_unavailable` with `Retry-After: 5`; `/readyz` 503, `/healthz` 200; verified on a real stack (PostgreSQL stopped and restarted, API recovered without restart) |
| Huge or malformed document | Rejected at intake (limits) or `document_too_complex` when the sandbox limit is hit |

## Likely bottlenecks (hypotheses to test, not findings)
1. OCR CPU per worker; scale worker replicas on queue depth and age.
2. The per-tenant row lock serialises one tenant's submissions; a very high single-tenant rate will queue on it.
3. Sandbox child startup adds per-document overhead (visible as slower tests); measure under load.
4. PostgreSQL connection pool size versus API and worker replicas; use PgBouncer for many replicas.
5. Redis rate-limit Lua cost grows with window size at high per-key rates.

## How to measure
Follow the header of `loadtest/locustfile.py`. Record: p95/p99 of `POST /bills`, share of 429/503, time to `completed`, queue age, worker CPU and memory, PostgreSQL connections and lock waits. Only then set plan limits and worker counts.
