"""Platform hardening: error contract, failure behaviour, fairness, fencing, correlation, logs, metrics, health."""
import io
import json
import logging
import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.context import request_id_var
from app.core.time import utcnow
from app.main import create_app
from app.models import AuditLog, Feedback, OcrResult, Plan, Process
from app.observability.logging import JsonFormatter
from app.ocr.base import OCRTransientError
from app.repositories.process import ProcessRepository
from app.services.audit import AuditService
from app.storage.base import StorageError
from app.workers.job import Outcome, WorkerContext, run_job
from tests.conftest import ADMIN, make_tenant
from tests.fakes import FakeOCR, pdf_bytes, png_bytes, unique_png
from tests.helpers import submit_and_process, upload


def op_error():
    return OperationalError("SELECT secret_table", {}, Exception("connection refused /var/run/pg.sock password=pw"))


def assert_error_shape(r, status=None):
    body = r.json()
    if status:
        assert r.status_code == status
    e = body["error"]
    assert set(e) >= {"code", "message", "request_id"} and e["request_id"] and r.headers["X-Request-ID"]
    for forbidden in ("Traceback", "sqlalchemy", "SELECT", "/var/", "password", "psycopg", "File \""):
        assert forbidden not in r.text


# ------------------------------------------------------------------ error contract
def test_error_contract_across_status_codes(client, container, tenant_a):
    ro = make_tenant(container, "ro", scopes=["bills:read"])
    free = make_tenant(container, "freeerr", plan="free")
    cases = [
        (client.get("/api/v1/bills/" + "0" * 20), 401),
        (upload(client, ro), 403),
        (client.get("/api/v1/bills/" + "0" * 20, headers=tenant_a.h), 404),
        (upload(client, tenant_a, b"MZ" + b"0" * 40), 415),
        (upload(client, tenant_a, b""), 422),
        (client.post("/api/v1/bills", headers=tenant_a.h), 422),
        (upload(client, free, b"\x89PNG\r\n\x1a\n" + b"0" * 250_000), 413),
        (client.get("/nope", headers=tenant_a.h), 404),
        (client.put("/api/v1/bills", headers=tenant_a.h), 405),
    ]
    for r, status in cases:
        assert_error_shape(r, status)
    for _ in range(6):
        r = client.get("/api/v1/bills/" + "0" * 20, headers=free.h)
    assert_error_shape(r, 429) and r.headers["Retry-After"]


def test_validation_errors_do_not_echo_input(client, tenant_a):
    secret = "TOP-SECRET-VALUE-123"
    r = client.post(f"/api/v1/bills/{'0'*20}/feedback", headers=tenant_a.h, json={"corrections": [{"field": secret * 10}]})
    assert r.status_code in (404, 422) and secret not in r.text


def test_auth_failures_are_indistinguishable(client, container, tenant_a):
    from app.models import ApiKey
    t_rev, t_exp = make_tenant(container, "rev"), make_tenant(container, "exp")
    client.post(f"/internal/v1/clients/{t_rev.client_id}/api-keys/{t_rev.key_id}/revoke", headers=ADMIN)
    with container.session_factory() as db:
        db.query(ApiKey).filter_by(id=t_exp.key_id).update({"expires_at": utcnow() - timedelta(seconds=5)}); db.commit()
    url = "/api/v1/bills/" + "0" * 20
    bodies = []
    for h in ({"X-API-Key": t_rev.api_key}, {"X-API-Key": t_exp.api_key}, {"X-API-Key": "ocr_" + "a" * 12 + "_" + "b" * 43}, {}):
        r = client.get(url, headers=h)
        b = r.json()["error"]; b.pop("request_id")
        bodies.append((r.status_code, b))
    assert len({json.dumps(b, sort_keys=True) for b in bodies}) == 1 and bodies[0][0] == 401


# ------------------------------------------------------------------ body size (chunked, no Content-Length)
def test_chunked_upload_over_limit_rejected(container, settings, tenant_a):
    settings.max_request_bytes = 50_000
    c = TestClient(create_app(container), raise_server_exceptions=False)
    def gen():
        for _ in range(20):
            yield b"x" * 10_000
    r = c.post("/api/v1/bills", content=gen(), headers={**tenant_a.h, "Content-Type": "multipart/form-data; boundary=zz"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


# ------------------------------------------------------------------ database / storage failure behaviour
def test_db_failure_during_upload_is_503_and_leaves_nothing(client, container, tenant_a, queue, monkeypatch):
    def boom(self, **kw): raise op_error()
    monkeypatch.setattr(AuditService, "record", boom)
    r = upload(client, tenant_a, unique_png(900))
    assert_error_shape(r, 503) and r.headers["Retry-After"]
    assert queue.items == []
    with container.session_factory() as db:
        assert db.query(Process).count() == 0
    assert list(container.storage.list_objects("tenants/")) == []          # uploaded object cleaned up


def test_commit_failure_during_upload_is_not_reported_as_success(client, container, tenant_a, queue):
    touched = set()
    def mark(session, ctx, instances):
        if any(isinstance(o, Process) for o in session.new):
            touched.add(id(session))
    def fail_commit(session):
        if id(session) in touched:
            raise op_error()
    event.listen(Session, "before_flush", mark)
    event.listen(Session, "before_commit", fail_commit)
    try:
        r = upload(client, tenant_a, unique_png(901))
    finally:
        event.remove(Session, "before_flush", mark)
        event.remove(Session, "before_commit", fail_commit)
    assert r.status_code == 503 and queue.items == []
    with container.session_factory() as db:
        assert db.query(Process).count() == 0 and db.query(AuditLog).filter_by(action="bills.submit").count() == 0
    assert list(container.storage.list_objects("tenants/")) == []


def test_post_commit_bookkeeping_failure_still_returns_202(client, tenant_a, queue, monkeypatch):
    monkeypatch.setattr(ProcessRepository, "mark_enqueued", lambda self, ids: (_ for _ in ()).throw(op_error()))
    r = upload(client, tenant_a, unique_png(902))
    assert r.status_code == 202 and len(queue.items) == 1          # durable + queued; reaper fixes enqueued_at


def test_db_down_on_read_is_503(client, tenant_a, monkeypatch):
    monkeypatch.setattr(ProcessRepository, "get", lambda self, *a, **k: (_ for _ in ()).throw(op_error()))
    r = client.get(f"/api/v1/bills/{'1'*20}", headers=tenant_a.h)
    assert_error_shape(r, 503) and r.json()["error"]["code"] == "database_unavailable"


def test_db_failure_during_feedback_stores_nothing(client, container, tenant_a, run_queue, monkeypatch):
    pid = submit_and_process(client, tenant_a, run_queue)
    def boom(self, **kw):
        if kw.get("action") == "feedback.submit":
            raise op_error()
    monkeypatch.setattr(AuditService, "record", boom)
    r = client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": [{"field": "total", "value": 1}]})
    assert_error_shape(r, 503)
    with container.session_factory() as db:
        assert db.query(Feedback).count() == 0


def test_storage_failure_on_upload_is_503_and_clean(client, container, tenant_a, queue, monkeypatch):
    monkeypatch.setattr(container.storage, "put", lambda *a, **k: (_ for _ in ()).throw(StorageError("s3 down: AKIAXXXX")))
    r = upload(client, tenant_a, unique_png(903))
    assert_error_shape(r, 503) and "AKIA" not in r.text and r.json()["error"]["code"] == "storage_unavailable"
    with container.session_factory() as db:
        assert db.query(Process).count() == 0


def test_delete_with_storage_failure_is_not_reported_as_deleted(client, container, tenant_a, run_queue, monkeypatch):
    pid = submit_and_process(client, tenant_a, run_queue)
    real = container.storage.delete
    monkeypatch.setattr(container.storage, "delete", lambda k: (_ for _ in ()).throw(StorageError("down")))
    r = client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h)
    assert_error_shape(r, 503)
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["status"] == "completed"   # still there, retryable
    monkeypatch.setattr(container.storage, "delete", real)
    assert client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 204
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 404


def test_worker_db_failure_at_result_persist_does_not_complete(client, container, tenant_a, queue, worker_ctx, monkeypatch):
    pid = upload(client, tenant_a).json()["process_id"]
    real, calls = ProcessRepository.transition, {"n": 0}
    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise op_error()                     # the COMPLETED transition blows up once
        return real(self, *a, **kw)
    monkeypatch.setattr(ProcessRepository, "transition", flaky)
    assert run_job(pid, worker_ctx).outcome is Outcome.RETRY
    with container.session_factory() as db:
        assert db.query(OcrResult).count() == 0 and db.get(Process, pid).status == "queued"
    assert run_job(pid, worker_ctx).outcome is Outcome.COMPLETED


# ------------------------------------------------------------------ worker fairness, fencing
def _set_plan(container, plan, **vals):
    with container.session_factory() as db:
        p = db.get(Plan, plan)
        for k, v in vals.items():
            setattr(p, k, v)
        db.commit()


def test_tenant_running_cap_defers_extra_jobs(client, container, worker_ctx, queue):
    _set_plan(container, "enterprise", max_running_jobs=1)
    a, b = make_tenant(container, "fa"), make_tenant(container, "fb")
    ids = [upload(client, a, unique_png(910 + i)).json()["process_id"] for i in range(3)]
    bid = upload(client, b, unique_png(920)).json()["process_id"]
    with container.session_factory() as db:          # a's first job is "running" on some other worker
        db.query(Process).filter_by(id=ids[0]).update({"status": "processing", "started_at": utcnow(), "attempts": 1}); db.commit()
    r = run_job(ids[1], worker_ctx)
    assert r.outcome is Outcome.DEFERRED and r.countdown > 0
    assert run_job(bid, worker_ctx).outcome is Outcome.COMPLETED            # other tenant is not blocked
    with container.session_factory() as db:
        assert db.get(Process, ids[1]).status == "queued" and db.get(Process, ids[1]).attempts == 0   # no attempt burned
        db.query(Process).filter_by(id=ids[0]).update({"status": "completed"}); db.commit()
    assert run_job(ids[1], worker_ctx).outcome is Outcome.COMPLETED


def test_presumed_dead_processing_rows_do_not_block_tenant(client, container, worker_ctx):
    _set_plan(container, "enterprise", max_running_jobs=1)
    t = make_tenant(container, "deadcap")
    a, b = (upload(client, t, unique_png(930 + i)).json()["process_id"] for i in range(2))
    with container.session_factory() as db:
        db.query(Process).filter_by(id=a).update({"status": "processing", "attempts": 1,
                                                  "started_at": utcnow() - timedelta(hours=2)}); db.commit()
    assert run_job(b, worker_ctx).outcome is Outcome.COMPLETED


def test_stale_worker_cannot_complete_a_reclaimed_job(client, container, tenant_a, queue, worker_ctx):
    """Worker A stalls; reaper requeues; worker B completes; A wakes up. A must not corrupt/duplicate the result."""
    pid = upload(client, tenant_a).json()["process_id"]
    good = WorkerContext(container.settings, container.session_factory, container.storage, FakeOCR(), worker_ctx.extractor)
    events = {}

    class StallingOCR(FakeOCR):
        def recognize(self, pages):
            with container.session_factory() as db:          # reaper fires while A is mid-flight
                ProcessRepository(db).transition(pid, "queued", sources=["processing"], error_code="worker_lost"); db.commit()
            events["b"] = run_job(pid, good).outcome          # worker B takes over and finishes
            return super().recognize(pages)

    slow = WorkerContext(container.settings, container.session_factory, container.storage, StallingOCR(), worker_ctx.extractor)
    assert run_job(pid, slow).outcome is Outcome.SKIPPED
    assert events["b"] is Outcome.COMPLETED
    with container.session_factory() as db:
        assert db.query(OcrResult).filter_by(process_id=pid).count() == 1
        p = db.get(Process, pid)
        assert p.status == "completed" and p.attempts == 2


def test_stale_worker_failure_cannot_fail_a_reclaimed_job(client, container, tenant_a, worker_ctx):
    pid = upload(client, tenant_a).json()["process_id"]
    good = WorkerContext(container.settings, container.session_factory, container.storage, FakeOCR(), worker_ctx.extractor)

    class StallThenFail(FakeOCR):
        def recognize(self, pages):
            with container.session_factory() as db:
                ProcessRepository(db).transition(pid, "queued", sources=["processing"]); db.commit()
            run_job(pid, good)
            raise OCRTransientError("late failure from the stale worker")

    slow = WorkerContext(container.settings, container.session_factory, container.storage, StallThenFail(), worker_ctx.extractor)
    assert run_job(pid, slow).outcome is Outcome.SKIPPED
    with container.session_factory() as db:
        assert db.get(Process, pid).status == "completed"


def test_duplicate_delivery_while_processing_is_skipped(client, container, tenant_a, worker_ctx):
    pid = upload(client, tenant_a).json()["process_id"]
    with container.session_factory() as db:
        db.query(Process).update({"status": "processing", "attempts": 1, "started_at": utcnow()}); db.commit()
    assert run_job(pid, worker_ctx).outcome is Outcome.SKIPPED


def test_deleted_process_is_never_processed(client, container, tenant_a, worker_ctx, ocr):
    pid = upload(client, tenant_a).json()["process_id"]
    assert client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 204
    assert run_job(pid, worker_ctx).outcome is Outcome.SKIPPED and ocr.calls == 0


# ------------------------------------------------------------------ queues, plan governance
def test_bulk_and_single_use_separate_queues_and_backpressure_is_isolated(client, tenant_a, queue):
    assert upload(client, tenant_a, unique_png(940)).status_code == 202
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h, files=[("files", ("a.png", unique_png(941), "image/png"))])
    single_id = queue.items[0]; bulk_id = r.json()["processes"][0]["process_id"]
    assert queue.bulk_flags[single_id] is False and queue.bulk_flags[bulk_id] is True
    queue.reported_bulk_depth = 10**9                                   # bulk queue saturated
    assert upload(client, tenant_a, unique_png(942)).status_code == 202   # single-document path unaffected
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h, files=[("files", ("a.png", unique_png(943), "image/png"))])
    assert r.status_code == 503 and r.headers["Retry-After"]


def test_reaper_requeues_bulk_jobs_on_bulk_queue(client, container, tenant_a, queue, worker_ctx):
    from app.workers.maintenance import reap_stale
    queue.fail = True
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h, files=[("files", ("a.png", unique_png(944), "image/png"))])
    pid = r.json()["processes"][0]["process_id"]
    queue.fail = False
    with container.session_factory() as db:
        db.query(Process).update({"created_at": utcnow() - timedelta(hours=1)}); db.commit()
    reap_stale(worker_ctx, queue)
    assert queue.bulk_flags[pid] is True


def test_bulk_endpoint_has_its_own_rate_bucket(client, container):
    _set_plan(container, "enterprise", bulk_requests_per_minute=2)
    t = make_tenant(container, "bulkrl")
    codes = [client.post("/api/v1/bills/bulk", headers=t.h, files=[("files", ("a.png", unique_png(950 + i), "image/png"))]).status_code
             for i in range(3)]
    assert codes == [202, 202, 429]
    assert upload(client, t, unique_png(960)).status_code == 202          # single uploads use a different bucket


def test_documents_per_minute_limit(client, container):
    _set_plan(container, "enterprise", documents_per_minute=3)
    t = make_tenant(container, "docrate")
    codes = [upload(client, t, unique_png(970 + i)).status_code for i in range(4)]
    assert codes == [202, 202, 202, 429]
    r = upload(client, t, unique_png(979))
    assert r.json()["error"]["code"] == "document_rate_exceeded" and 1 <= int(r.headers["Retry-After"]) <= 61


def test_max_pages_per_document_from_plan(client, container):
    _set_plan(container, "enterprise", max_pages_per_document=2)
    t = make_tenant(container, "pages")
    r = upload(client, t, pdf_bytes(3), "a.pdf", "application/pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "pdf_page_limit"
    assert upload(client, t, pdf_bytes(2), "a.pdf", "application/pdf").status_code == 202


# ------------------------------------------------------------------ correlation, logs, metrics, versions
def _capture_logs():
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setFormatter(JsonFormatter())
    root = logging.getLogger(); root.addHandler(h); root.setLevel(logging.DEBUG)
    return buf, lambda: root.removeHandler(h)


def test_request_id_flows_request_to_process_to_worker_logs(client, container, tenant_a, run_queue):
    buf, stop = _capture_logs()
    try:
        r = upload(client, tenant_a, headers={"X-Request-ID": "trace-corr-12345"})
        pid = r.json()["process_id"]
        run_queue()
    finally:
        stop()
    with container.session_factory() as db:
        assert db.get(Process, pid).request_id == "trace-corr-12345"
    lines = [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]
    job = [l for l in lines if l["logger"] == "app.workers.job" and l["process_id"] == pid]
    assert {l["msg"] for l in job} >= {"job started", "job completed"}
    assert all(l["request_id"] == "trace-corr-12345" and l["tenant_id"] == tenant_a.client_id for l in job)
    done = next(l for l in job if l["msg"] == "job completed")
    assert done["ocr_model"] == "fake-ocr@1.0" and done["extractor_model"].startswith("rule-based") and done["preprocessing_version"] == "1"
    assert all(k in done for k in ("ocr_ms", "extraction_ms", "total_ms", "render_ms"))


def test_logs_never_contain_keys_headers_or_document_text(client, tenant_a, run_queue):
    buf, stop = _capture_logs()
    try:
        client.get("/api/v1/bills/" + "0" * 20, headers={"X-API-Key": tenant_a.api_key + "zz", "Authorization": "Bearer SUPERSECRETTOKEN"})
        upload(client, tenant_a)
        client.get("/metrics", headers={"Authorization": "Bearer METRICSECRET999"})
        run_queue()
    finally:
        stop()
    out = buf.getvalue()
    secret_part = tenant_a.api_key.split("_", 2)[2]
    for needle in (secret_part, "SUPERSECRETTOKEN", "METRICSECRET999", "Himalayan", "INV-2026", "4,746"):
        assert needle not in out
    assert out.strip()                                                 # we did capture logs


def test_worker_metrics_present_and_low_cardinality(client, tenant_a, run_queue):
    from prometheus_client import generate_latest
    submit_and_process(client, tenant_a, run_queue)
    text = generate_latest().decode()
    for name in ("ocr_jobs_received_total", "ocr_jobs_total", "ocr_job_duration_seconds", "ocr_engine_duration_seconds",
                 "extraction_duration_seconds", "ocr_queue_latency_seconds", "ocr_worker_active_jobs", "ocr_worker_errors_total",
                 "documents_submitted_total", "http_requests_total", "rate_limit_rejections_total", "quota_rejections_total",
                 "ocr_jobs_deferred_total"):
        assert name in text, name
    assert 'ocr_jobs_total{outcome="completed"}' in text
    import re
    # Route templates such as /api-keys/{key_id}/revoke are fine; concrete identifiers are not.
    assert not re.search(r"\b(proc|cli|key)_[0-9a-f]{12,}", text)
    for forbidden in ("ocr_0", "inv.png", "bill.png"):
        assert forbidden not in text


def test_result_exposes_all_version_fields_and_provisional_confidence(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    b = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert b["model_versions"] == {"ocr": "fake-ocr@1.0", "extractor": "rule-based-baseline@0.1.0"}
    # Prompt 3: schema v2 (additive) and extra stage versions; the original keys remain and are still present.
    pv = b["pipeline_versions"]
    assert pv["preprocessing"] == "1" and pv["schema_version"] == "2"
    assert {"normalization", "validation", "layout", "confidence_method"} <= set(pv)
    assert b["confidence_calibrated"] is False


# ------------------------------------------------------------------ health, metrics endpoint, docs
def test_liveness_is_dependency_free_and_readiness_reports_each_dependency(container, settings, tenant_a, monkeypatch):
    c = TestClient(create_app(container))
    monkeypatch.setattr(container.storage, "ping", lambda: (_ for _ in ()).throw(StorageError("x")))
    assert c.get("/healthz").status_code == 200
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["storage"] == "unavailable" and r.json()["checks"]["database"] == "ok"


def test_metrics_endpoint_requires_token_when_configured(container, settings):
    from pydantic import SecretStr
    settings.metrics_token = SecretStr("m" * 32)
    c = TestClient(create_app(container))
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer " + "m" * 32}).status_code == 200


def test_openapi_documents_real_surface_only(client):
    spec = client.get("/openapi.json").json()
    original = {
        "/api/v1/bills", "/api/v1/bills/bulk", "/api/v1/bills/batches/{batch_id}", "/api/v1/bills/{process_id}",
        "/api/v1/bills/{process_id}/cancel", "/api/v1/bills/{process_id}/feedback",
        "/internal/v1/clients", "/internal/v1/clients/{client_id}/suspend", "/internal/v1/clients/{client_id}/activate",
        "/internal/v1/clients/{client_id}/api-keys", "/internal/v1/clients/{client_id}/api-keys/{key_id}/revoke",
        "/internal/v1/clients/{client_id}/api-keys/{key_id}/rotate"}
    # v0.3 contract change (additive): operator-only ML endpoints. Nothing public (/api/v1) was added or removed, and
    # every path that existed before is still present. Any other new path still fails this test.
    ml_internal = {
        "/internal/v1/clients/{client_id}/training-policy", "/internal/v1/datasets", "/internal/v1/datasets/{dataset_id}",
        "/internal/v1/examples", "/internal/v1/examples/{example_id}/review", "/internal/v1/models",
        "/internal/v1/models/{model_id}/approve", "/internal/v1/models/{model_id}/evaluations",
        "/internal/v1/models/{model_id}/promote", "/internal/v1/models/{model_id}/retire",
        "/internal/v1/models/{model_id}/rollback"}
    # Additive (review web layer): GET /api/v1/bills (list) shares the existing path; GET /api/v1/bills/{process_id}/file is new.
    assert set(spec["paths"]) == original | ml_internal | {"/api/v1/bills/{process_id}/file"}
    assert spec["components"]["securitySchemes"]["ApiKey"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    post = spec["paths"]["/api/v1/bills"]["post"]
    assert {"202", "401", "413", "415", "422", "429", "503"} <= set(post["responses"])
    assert "Idempotency-Key" in [p["name"] for p in post["parameters"]]
    assert "429" in spec["paths"]["/api/v1/bills/bulk"]["post"]["responses"]


def test_every_api_route_requires_authentication(container):
    """Strengthened in the v0.3 audit: with FastAPI 0.142 `app.routes` holds lazy included routers (empty paths), so the
    original structural loop examined zero routes and could never fail. Operations are now taken from the OpenAPI document and
    each one must answer 401 without credentials (see also tests/integration/test_final_audit.py)."""
    container.settings.auth_failures_per_minute = 10_000      # the failed-auth throttle is tested elsewhere
    client = TestClient(create_app(container), raise_server_exceptions=False)
    spec = client.get("/openapi.json").json()
    checked = 0
    for path, ops in spec["paths"].items():
        for method in ops:
            url = path.replace("{process_id}", "0" * 20).replace("{batch_id}", "batch_" + "0" * 32) \
                .replace("{client_id}", "cli_x").replace("{key_id}", "key_x").replace("{example_id}", "ex_x") \
                .replace("{dataset_id}", "ds_x").replace("{model_id}", "mv_x")
            assert client.request(method.upper(), url, json={}).status_code == 401, (method, path)
            checked += 1
    assert checked >= 25
