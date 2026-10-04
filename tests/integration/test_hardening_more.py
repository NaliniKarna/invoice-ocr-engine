"""Second hardening batch: tenant enumeration, admin scoping, bulk idempotency isolation, storage cleanup, retention,
settings validation, migration upgrade path, index usage, tracing, and PostgreSQL-only races."""
import os
import threading
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.core.time import utcnow
from app.main import create_app
from app.models import AuditLog, Feedback, Process
from app.workers.job import run_job
from app.workers.maintenance import purge_audit_logs, sweep_orphans
from tests.conftest import ADMIN, PG_URL, make_tenant
from tests.fakes import unique_png
from tests.helpers import submit_and_process, upload

needs_pg = pytest.mark.skipif(not PG_URL, reason="needs real PostgreSQL")


# ------------------------------------------------------------------ tenant isolation
def test_unknown_and_foreign_ids_are_indistinguishable(client, tenant_a, tenant_b, run_queue):
    """A foreign process must be byte-identical to a nonexistent one, on every verb (no existence oracle)."""
    foreign = submit_and_process(client, tenant_b, run_queue)
    missing = "0" * 20
    calls = [("GET", "/api/v1/bills/{}", None), ("DELETE", "/api/v1/bills/{}", None),
             ("POST", "/api/v1/bills/{}/cancel", None),
             ("POST", "/api/v1/bills/{}/feedback", {"corrections": [{"field": "total", "value": 1}]})]
    for method, path, body in calls:
        outs = []
        for pid in (foreign, missing):
            r = client.request(method, path.format(pid), headers=tenant_a.h, json=body)
            err = r.json()["error"]
            outs.append((r.status_code, err["code"], err["message"]))
        assert outs[0] == outs[1], (method, path, outs)
        assert outs[0][0] == 404


def test_admin_revoke_is_scoped_to_the_named_client(client, tenant_a, tenant_b):
    r = client.post(f"/internal/v1/clients/{tenant_a.client_id}/api-keys/{tenant_b.key_id}/revoke", headers=ADMIN)
    assert r.status_code == 404
    assert upload(client, tenant_b).status_code == 202          # B's key is untouched
    ok = client.post(f"/internal/v1/clients/{tenant_b.client_id}/api-keys/{tenant_b.key_id}/revoke", headers=ADMIN)
    assert ok.status_code == 200
    assert upload(client, tenant_b).status_code == 401


def test_admin_endpoints_reject_tenant_api_key(client, tenant_a):
    r = client.post(f"/internal/v1/clients/{tenant_a.client_id}/api-keys/{tenant_a.key_id}/revoke", headers=tenant_a.h)
    assert r.status_code == 401


# ------------------------------------------------------------------ idempotency
def test_bulk_idempotency_key_is_not_shared_between_tenants(client, tenant_a, tenant_b, queue):
    hdr = {"Idempotency-Key": "shared-key-000001"}
    files = lambda: [("files", ("a.png", unique_png(4242), "image/png"))]
    ra = client.post("/api/v1/bills/bulk", files=files(), headers={**tenant_a.h, **hdr})
    rb = client.post("/api/v1/bills/bulk", files=files(), headers={**tenant_b.h, **hdr})
    assert ra.status_code == rb.status_code == 202
    assert ra.json()["batch_id"] != rb.json()["batch_id"]
    again = client.post("/api/v1/bills/bulk", files=files(), headers={**tenant_a.h, **hdr})
    assert again.json()["batch_id"] == ra.json()["batch_id"]      # same tenant replays
    assert len(queue.items) == 2


# ------------------------------------------------------------------ storage cleanup and retention
def _age_object(storage, key, hours):
    """LocalFsStorage derives last_modified from mtime."""
    path = storage._path(key) if hasattr(storage, "_path") else None
    if path is None:
        pytest.skip("storage backend does not expose a file path to age")
    ts = (utcnow() - timedelta(hours=hours)).timestamp()
    os.utime(path, (ts, ts))


def test_orphan_sweep_removes_only_old_unreferenced_objects(client, container, tenant_a, worker_ctx):
    pid = upload(client, tenant_a, unique_png(7001)).json()["process_id"]
    with container.session_factory() as db:
        live_key = db.get(Process, pid).storage_key
    orphan_old = f"tenants/{tenant_a.client_id}/documents/proc_{'a' * 32}/orphan_old"
    orphan_new = f"tenants/{tenant_a.client_id}/documents/proc_{'b' * 32}/orphan_new"
    for k in (orphan_old, orphan_new):
        container.storage.put(k, b"x", "application/octet-stream")
    _age_object(container.storage, orphan_old, 5)
    _age_object(container.storage, live_key, 5)
    removed = sweep_orphans(worker_ctx)
    keys = {o.key for o in container.storage.list_objects("tenants/")}
    assert removed == 1
    assert orphan_old not in keys          # old and unreferenced: removed
    assert orphan_new in keys              # within grace period: could be an in-flight upload
    assert live_key in keys                # referenced by a live process: kept


def test_audit_logs_are_purged_after_retention_in_bounded_batches(container, worker_ctx):
    old = utcnow() - timedelta(days=worker_ctx.settings.audit_retention_days + 5)
    with container.session_factory() as db:
        for i in range(5):
            db.add(AuditLog(id=f"aud_old{i}", actor="t", action="x", resource_type="t", resource_id="r", created_at=old))
        db.add(AuditLog(id="aud_new", actor="t", action="x", resource_type="t", resource_id="r"))
        db.commit()
    assert purge_audit_logs(worker_ctx, batch=3) == 3            # bounded
    assert purge_audit_logs(worker_ctx, batch=3) == 2
    with container.session_factory() as db:
        assert [a.id for a in db.query(AuditLog).all()] == ["aud_new"]


# ------------------------------------------------------------------ settings validation
def _prod(**kw):
    base = dict(env="prod", database_url="postgresql+psycopg://svc:s3cret@db.internal/ocr", storage_backend="s3",
                api_key_pepper="p" * 40, admin_token="a" * 40, metrics_token="m" * 40, s3_access_key="AKIAREAL",
                s3_secret_key="realsecret", s3_sse="AES256", ocr_provider="tesseract", cors_allow_origins=[])
    base.update(kw)
    return Settings(**base)


def test_valid_prod_settings_load():
    _prod()


@pytest.mark.parametrize("override", [
    {"api_key_pepper": "short"}, {"admin_token": "short"}, {"storage_backend": "local"},
    {"cors_allow_origins": ["*"]}, {"database_url": "postgresql+psycopg://ocr:ocr@db/ocr"},
    {"s3_access_key": "minioadmin"}, {"s3_sse": None}, {"metrics_token": None}, {"ocr_provider": "unconfigured"},
    {"stale_processing_seconds": 10},      # shorter than the task time limit: reaper would race live workers
])
def test_unsafe_prod_settings_refuse_to_start(override):
    with pytest.raises(Exception):
        _prod(**override)


# ------------------------------------------------------------------ PostgreSQL-only
@needs_pg
def test_migration_upgrade_from_0001_preserves_existing_rows():
    """Build a database at revision 0001, insert a tenant and a process, upgrade to head, and check data survives."""
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    admin = sa.create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    name = "mig_" + uuid.uuid4().hex[:8]
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = PG_URL.rsplit("/", 1)[0] + "/" + name
    old = os.environ.get("OCR_DATABASE_URL")
    os.environ["OCR_DATABASE_URL"] = url
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "0001")
        eng = sa.create_engine(url)
        with eng.begin() as c:
            plan = c.execute(text("SELECT id FROM plans LIMIT 1")).scalar()
            c.execute(text("INSERT INTO clients (id, name, plan_id, status, training_opt_in, created_at) "
                           "VALUES ('cli_mig', 'm', :p, 'active', false, now())"), {"p": plan})
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM clients WHERE id='cli_mig'")).scalar() == 1
            cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='processes'"))}
            assert "request_id" in cols
        command.downgrade(cfg, "0001")
        command.upgrade(cfg, "head")
        eng.dispose()
    finally:
        if old is not None:
            os.environ["OCR_DATABASE_URL"] = old
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@needs_pg
def test_hot_queries_use_indexes(container, tenant_a):
    """The queries on the hot path must not sequential-scan once the table is non-trivial."""
    with container.session_factory() as db:
        db.execute(text("""INSERT INTO processes (id, client_id, status, storage_key, content_type, file_size, sha256,
                           original_filename, created_at, updated_at, attempts, max_attempts, enqueued_at, client_metadata)
                           SELECT 'proc_ix' || g, :c, CASE WHEN g % 50 = 0 THEN 'queued' ELSE 'completed' END, 'k' || g,
                           'image/png', 1, 'h' || g, 'f.png', now() - (g || ' seconds')::interval, now(), 0, 3, now(), '{}'::jsonb
                           FROM generate_series(1, 20000) g"""), {"c": tenant_a.client_id})
        db.execute(text("ANALYZE processes"))
        db.execute(text("SET LOCAL enable_seqscan = off"))   # planner must have a usable index, not merely prefer one
        plans = {
            "get": "SELECT * FROM processes WHERE client_id = :c AND id = 'proc_ix77'",
            "list": "SELECT * FROM processes WHERE client_id = :c ORDER BY created_at DESC LIMIT 20",
            "stale_queued": "SELECT id FROM processes WHERE status = 'queued' AND enqueued_at < now() - interval '1 hour'",
        }
        for name, q in plans.items():
            plan = "\n".join(r[0] for r in db.execute(text("EXPLAIN " + q), {"c": tenant_a.client_id}))
            assert "Seq Scan" not in plan, (name, plan)


@needs_pg
def test_parallel_feedback_submissions_store_each_exactly_once(container, tenant_a, run_queue):
    client = TestClient(create_app(container))
    pid = submit_and_process(client, tenant_a, run_queue)
    body = {"corrections": [{"field": "total", "value": 123.45}]}
    bar, out = threading.Barrier(6), [None] * 6

    def go(i):
        bar.wait()
        out[i] = client.post(f"/api/v1/bills/{pid}/feedback", json=body, headers=tenant_a.h)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert all(r.status_code in (200, 201, 202) for r in out), [r.text for r in out]
    with container.session_factory() as db:
        assert db.query(Feedback).filter_by(process_id=pid).count() == 6   # one row per accepted submission, none lost/duplicated


@needs_pg
def test_daily_quota_cannot_be_bypassed_by_parallel_uploads(container):
    from app.models import Plan
    with container.session_factory() as db:
        p = db.get(Plan, "free")
        p.upload_requests_per_minute = p.requests_per_minute = p.tenant_requests_per_minute = 1000
        p.max_concurrent_jobs = 1000
        p.daily_quota = 6
        db.commit()
    free = make_tenant(container, "quota-racer", plan="free")
    client = TestClient(create_app(container))
    bar, out = threading.Barrier(16), [None] * 16

    def go(i):
        bar.wait()
        out[i] = upload(client, free, unique_png(9000 + i))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    [t.start() for t in ts]; [t.join() for t in ts]
    codes = [r.status_code for r in out]
    assert codes.count(202) == 6, codes
    assert all(c == 429 for c in codes if c != 202), codes
    with container.session_factory() as db:
        assert db.query(Process).filter_by(client_id=free.client_id).count() == 6


# ------------------------------------------------------------------ tracing
def test_tracing_is_noop_when_disabled(client, tenant_a):
    from app.observability import tracing
    tracing.reset_for_tests()
    assert tracing.inject_headers() == {}
    assert upload(client, tenant_a).status_code == 202


def test_trace_context_propagates_from_request_to_worker(container, tenant_a, queue, worker_ctx):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from app.observability import tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.reset_for_tests()
    assert tracing.setup_tracing(enabled=True, service_name="test", provider=provider)
    try:
        carriers = {}
        orig = queue.enqueue
        def capture(pid, **kw):
            carriers[pid] = tracing.inject_headers()      # what the Celery queue attaches as message headers
            orig(pid, **kw)
        queue.enqueue = capture
        client = TestClient(create_app(container), raise_server_exceptions=False)
        assert upload(client, tenant_a, unique_png(31337)).status_code == 202
        pid = queue.items.pop(0)
        assert carriers[pid].get("traceparent")
        run_job(pid, worker_ctx, carriers[pid])
        spans = exporter.get_finished_spans()
        names = {s.name for s in spans}
        assert "http.request" in names and "job.run" in names
        assert len({s.context.trace_id for s in spans}) == 1, names       # one trace from HTTP request through the worker
        blob = repr([dict(s.attributes or {}) for s in spans])
        assert tenant_a.api_key not in blob
    finally:
        tracing.reset_for_tests()


def test_readyz_reports_ok_status_when_dependencies_are_healthy(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"          # regression: body used to say "degraded" on a healthy 200
