import json
import threading
from datetime import timedelta

import pytest

from app.core.time import utcnow
from app.models import IdempotencyKey, Process
from tests.conftest import make_tenant
from tests.fakes import png_bytes, unique_png
from tests.helpers import upload


# ---------------------------------------------------------------- idempotency
def test_retry_with_same_key_does_not_duplicate(client, tenant_a, queue, container):
    h = {"Idempotency-Key": "upload-key-0001"}
    r1 = upload(client, tenant_a, unique_png(10), headers=h)
    r2 = upload(client, tenant_a, unique_png(10), headers=h)
    assert r1.status_code == r2.status_code == 202 and r1.json() == r2.json()
    assert r2.headers["Idempotent-Replay"] == "true" and "Idempotent-Replay" not in r1.headers
    assert len(queue.items) == 1
    with container.session_factory() as db:
        assert db.query(Process).count() == 1


def test_same_key_different_payload_rejected(client, tenant_a, queue):
    h = {"Idempotency-Key": "upload-key-0002"}
    upload(client, tenant_a, unique_png(11), headers=h)
    r = upload(client, tenant_a, unique_png(12), headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_mismatch"
    assert len(queue.items) == 1


def test_expired_key_can_be_reused(client, tenant_a, container, queue):
    h = {"Idempotency-Key": "upload-key-0003"}
    upload(client, tenant_a, unique_png(13), headers=h)
    with container.session_factory() as db:
        db.query(IdempotencyKey).update({"expires_at": utcnow() - timedelta(seconds=1)}); db.commit()
    r = upload(client, tenant_a, unique_png(14), headers=h)
    assert r.status_code == 202 and len(queue.items) == 2


def test_invalid_key_format(client, tenant_a):
    assert upload(client, tenant_a, headers={"Idempotency-Key": "short"}).status_code == 422
    assert upload(client, tenant_a, headers={"Idempotency-Key": "has space in it!!"}).status_code == 422


def test_failed_validation_is_not_cached(client, tenant_a):
    h = {"Idempotency-Key": "upload-key-0004"}
    assert upload(client, tenant_a, b"garbage", headers=h).status_code == 415
    assert upload(client, tenant_a, unique_png(15), headers=h).status_code == 202


def test_replay_survives_enqueue_failure(client, tenant_a, queue):
    queue.fail = True
    r = upload(client, tenant_a, unique_png(16), headers={"Idempotency-Key": "upload-key-0005"})
    assert r.status_code == 202          # durably accepted; reaper will enqueue
    assert queue.items == []


def test_bulk_idempotent(client, tenant_a, queue):
    files = lambda: [("files", (f"{i}.png", unique_png(20 + i), "image/png")) for i in range(3)]
    h = {**tenant_a.h, "Idempotency-Key": "bulk-key-00001"}
    a = client.post("/api/v1/bills/bulk", files=files(), headers=h)
    b = client.post("/api/v1/bills/bulk", files=files(), headers=h)
    assert a.json() == b.json() and b.headers["Idempotent-Replay"] == "true" and len(queue.items) == 3


# ---------------------------------------------------------------- bulk
def test_bulk_happy_path(client, tenant_a, queue, run_queue):
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h,
                    files=[("files", (f"{i}.png", unique_png(30 + i), "image/png")) for i in range(4)])
    body = r.json()
    assert r.status_code == 202 and body["batch_id"].startswith("batch_") and body["accepted"] == 4
    assert len({p["process_id"] for p in body["processes"]}) == 4
    assert r.headers["Location"].endswith(body["batch_id"])
    assert len(queue.items) == 4
    run_queue()
    b = client.get(f"/api/v1/bills/batches/{body['batch_id']}", headers=tenant_a.h).json()
    assert b["status"] == "completed" and b["counts"]["completed"] == 4


def test_bulk_partial_failure_does_not_fail_batch(client, tenant_a, queue):
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h, files=[
        ("files", ("ok1.png", unique_png(40), "image/png")),
        ("files", ("bad.png", b"not an image", "image/png")),
        ("files", ("ok2.png", unique_png(41), "image/png"))])
    body = r.json()
    assert r.status_code == 202 and body["accepted"] == 2 and body["rejected"] == 1
    st = [p["status"] for p in body["processes"]]
    assert st == ["queued", "rejected", "queued"]
    assert body["processes"][1]["error"]["code"] == "unsupported_file_type" and body["processes"][1]["process_id"] is None
    assert len(queue.items) == 2


def test_bulk_all_invalid_is_422_with_details(client, tenant_a):
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h, files=[("files", ("a.png", b"x", "image/png"))])
    assert r.status_code == 422 and r.json()["error"]["details"][0]["index"] == 0


def test_bulk_batch_size_limit(client, container):
    free = make_tenant(container, "free-b", plan="free")  # max_batch_size=5
    r = client.post("/api/v1/bills/bulk", headers=free.h,
                    files=[("files", (f"{i}.png", unique_png(50 + i), "image/png")) for i in range(6)])
    assert r.status_code == 413 and r.json()["error"]["code"] == "batch_too_large"


def test_bulk_backpressure_partial_acceptance(client, container, queue):
    free = make_tenant(container, "free-c", plan="free")  # max_concurrent_jobs=4, batch<=5
    r = client.post("/api/v1/bills/bulk", headers=free.h,
                    files=[("files", (f"{i}.png", unique_png(60 + i), "image/png")) for i in range(5)])
    body = r.json()
    assert r.status_code == 202 and body["accepted"] == 4 and body["rejected"] == 1
    last = body["processes"][4]
    assert last["status"] == "rejected" and last["error"]["retryable"] is True
    assert last["error"]["code"] == "concurrency_limit_exceeded"


# ---------------------------------------------------------------- quotas, concurrency, rate limits
def test_concurrency_limit_429_with_retry_after(client, container):
    free = make_tenant(container, "free-d", plan="free")
    for i in range(3):
        assert upload(client, free, unique_png(70 + i)).status_code == 202   # upload rpm = 3
    # rate limiter (3/min upload) trips first on the 4th:
    r = upload(client, free, unique_png(79))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited_upload"


def test_active_job_limit(client, container):
    from app.models import Plan
    with container.session_factory() as db:
        db.get(Plan, "free").upload_requests_per_minute = 100
        db.get(Plan, "free").requests_per_minute = 100
        db.get(Plan, "free").tenant_requests_per_minute = 100
        db.commit()
    free = make_tenant(container, "free-e", plan="free")
    codes = [upload(client, free, unique_png(80 + i)).status_code for i in range(5)]
    assert codes == [202, 202, 202, 202, 429]
    r = upload(client, free, unique_png(90))
    assert r.json()["error"]["code"] == "concurrency_limit_exceeded" and int(r.headers["Retry-After"]) > 0


def test_daily_quota_and_completing_jobs_does_not_refund(client, container, run_queue):
    from app.models import Plan
    with container.session_factory() as db:
        p = db.get(Plan, "free"); p.upload_requests_per_minute = p.requests_per_minute = p.tenant_requests_per_minute = 100; db.commit()
    free = make_tenant(container, "free-f", plan="free")          # daily_quota=6
    for i in range(6):
        assert upload(client, free, unique_png(100 + i)).status_code == 202
        if i % 3 == 2:
            run_queue()                                          # frees concurrency; quota stays consumed
    r = upload(client, free, unique_png(110))
    assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded" and int(r.headers["Retry-After"]) > 60


def test_per_key_rate_limit_and_headers(client, container):
    free = make_tenant(container, "free-g", plan="free")          # 5 rpm per key
    rs = [client.get(f"/api/v1/bills/{'0'*20}", headers=free.h) for _ in range(6)]
    assert [r.status_code for r in rs[:5]] == [404] * 5
    assert rs[0].headers["X-RateLimit-Limit"] == "5" and rs[0].headers["X-RateLimit-Remaining"] == "4"
    assert rs[5].status_code == 429 and rs[5].json()["error"]["code"] == "rate_limited_key"
    assert "Retry-After" in rs[5].headers


def test_limits_are_per_tenant_not_global(client, container):
    a, b = make_tenant(container, "fa", plan="free"), make_tenant(container, "fb", plan="free")
    for _ in range(6): client.get(f"/api/v1/bills/{'0'*20}", headers=a.h)
    assert client.get(f"/api/v1/bills/{'0'*20}", headers=b.h).status_code == 404


def test_ip_rate_limit_precedes_auth(container, settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings.ip_requests_per_minute = 3
    c = TestClient(create_app(container), raise_server_exceptions=False)
    codes = [c.get("/api/v1/bills/" + "0" * 20).status_code for _ in range(5)]
    assert codes[:3] == [401] * 3 and codes[3:] == [429, 429]


def test_queue_saturation_returns_503(client, tenant_a, queue):
    queue.reported_depth = 10**9
    r = upload(client, tenant_a)
    assert r.status_code == 503 and r.headers["Retry-After"] == "30"


def test_rate_limiter_down_fails_closed(container, tenant_a):
    from fastapi.testclient import TestClient
    from app.main import create_app
    class Boom:
        def register_script(self, s):
            def f(**k): raise ConnectionError()
            return f
    from app.security.ratelimit import RateLimiter
    container.limiter = RateLimiter(Boom())
    r = TestClient(create_app(container), raise_server_exceptions=False).get("/api/v1/bills/" + "0" * 20, headers=tenant_a.h)
    assert r.status_code == 503
