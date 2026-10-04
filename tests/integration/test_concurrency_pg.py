"""Race conditions that SQLite cannot exhibit. Run with OCR_TEST_DATABASE_URL (real PostgreSQL)."""
import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Process
from tests.conftest import make_tenant
from tests.fakes import unique_png
from tests.helpers import upload

pytestmark = pytest.mark.skipif(not os.environ.get("OCR_TEST_DATABASE_URL"), reason="needs real PostgreSQL")


def _parallel(n, fn):
    out, bar = [None] * n, threading.Barrier(n)
    def run(i):
        bar.wait(); out[i] = fn(i)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return out


def test_parallel_identical_idempotent_requests_create_one_process(container, tenant_a):
    client = TestClient(create_app(container))
    data = unique_png(500)
    rs = _parallel(8, lambda i: upload(client, tenant_a, data, headers={"Idempotency-Key": "race-key-000001"}))
    assert {r.status_code for r in rs} == {202}
    assert len({r.json()["process_id"] for r in rs}) == 1
    with container.session_factory() as db:
        assert db.query(Process).count() == 1


def test_parallel_uploads_cannot_exceed_concurrency_limit(container):
    from app.models import Plan
    with container.session_factory() as db:
        p = db.get(Plan, "free"); p.upload_requests_per_minute = p.requests_per_minute = p.tenant_requests_per_minute = 1000; db.commit()
    free = make_tenant(container, "racer", plan="free")      # max_concurrent_jobs = 4
    client = TestClient(create_app(container))
    rs = _parallel(10, lambda i: upload(client, free, unique_png(600 + i)))
    codes = sorted(r.status_code for r in rs)
    assert codes.count(202) == 4 and codes.count(429) == 6
    with container.session_factory() as db:
        assert db.query(Process).filter_by(client_id=free.client_id).count() == 4


def test_parallel_workers_claim_a_job_exactly_once(container, tenant_a, queue, worker_ctx, ocr):
    client = TestClient(create_app(container))
    pid = upload(client, tenant_a).json()["process_id"]
    from app.workers.job import run_job
    res = _parallel(6, lambda i: run_job(pid, worker_ctx))
    assert sorted(r.outcome.value for r in res).count("completed") == 1 and ocr.calls == 1
