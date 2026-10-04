from datetime import timedelta

from app.core.time import utcnow
from app.models import ApiKey, AuditLog
from tests.conftest import ADMIN, make_tenant
from tests.helpers import upload


def test_missing_and_invalid_key_401_without_leaks(client):
    for h in ({}, {"X-API-Key": "nope"}, {"X-API-Key": "ocr_" + "0" * 12 + "_" + "a" * 43}):
        r = client.get("/api/v1/bills/" + "0" * 20, headers=h)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key" and r.json()["error"]["request_id"]
        assert "Traceback" not in r.text


def test_key_in_query_string_is_not_accepted(client, tenant_a):
    r = client.get(f"/api/v1/bills/{'0'*20}?api_key={tenant_a.api_key}")
    assert r.status_code == 401


def test_revoked_key_rejected(client, container, tenant_a):
    assert upload(client, tenant_a).status_code == 202
    r = client.post(f"/internal/v1/clients/{tenant_a.client_id}/api-keys/{tenant_a.key_id}/revoke", headers=ADMIN)
    assert r.status_code == 200
    assert upload(client, tenant_a).status_code == 401


def test_expired_key_rejected(client, container):
    t = make_tenant(container, "exp", expires_in_days=1)
    with container.session_factory() as db:
        db.query(ApiKey).filter_by(id=t.key_id).update({"expires_at": utcnow() - timedelta(seconds=1)}); db.commit()
    assert upload(client, t).status_code == 401


def test_scopes_enforced(client, container):
    ro = make_tenant(container, "ro", scopes=["bills:read"])
    r = upload(client, ro)
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    wo = make_tenant(container, "wo", scopes=["bills:write"])
    pid = upload(client, wo).json()["process_id"]
    assert client.get(f"/api/v1/bills/{pid}", headers=wo.h).status_code == 403
    assert client.post(f"/api/v1/bills/{pid}/feedback", json={"corrections": [{"field": "total", "value": 1}]},
                       headers=wo.h).status_code == 403


def test_suspended_client_blocked(client, container, tenant_a):
    client.post(f"/internal/v1/clients/{tenant_a.client_id}/suspend", headers=ADMIN)
    r = upload(client, tenant_a)
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_suspended"


def test_rotation_grace_then_expiry(client, container, tenant_a):
    r = client.post(f"/internal/v1/clients/{tenant_a.client_id}/api-keys/{tenant_a.key_id}/rotate",
                    json={"grace_seconds": 3600}, headers=ADMIN)
    assert r.status_code == 201
    new_key = r.json()["api_key"]
    assert upload(client, tenant_a).status_code == 202            # old key still valid during grace
    assert client.post("/api/v1/bills", files={"file": ("a.png", __import__("tests.fakes", fromlist=["x"]).png_bytes(), "image/png")},
                       headers={"X-API-Key": new_key}).status_code == 202
    with container.session_factory() as db:
        db.query(ApiKey).filter_by(id=tenant_a.key_id).update({"expires_at": utcnow() - timedelta(seconds=1)}); db.commit()
    assert upload(client, tenant_a).status_code == 401


def test_plaintext_key_never_stored_or_returned_again(client, container, tenant_a):
    with container.session_factory() as db:
        k = db.get(ApiKey, tenant_a.key_id)
        assert tenant_a.api_key not in (k.key_hash, k.prefix) and tenant_a.api_key not in str(k.__dict__)
        logs = db.query(AuditLog).all()
        assert all(tenant_a.api_key not in str(l.details) for l in logs)


def test_admin_api_requires_token(client):
    assert client.post("/internal/v1/clients", json={"name": "x"}).status_code == 401
    assert client.post("/internal/v1/clients", json={"name": "x"}, headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_brute_force_is_throttled(client):
    codes = [client.get("/api/v1/bills/" + "0" * 20, headers={"X-API-Key": "bad"}).status_code for _ in range(8)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:]


def test_last_used_updated(client, container, tenant_a):
    upload(client, tenant_a)
    with container.session_factory() as db:
        assert db.get(ApiKey, tenant_a.key_id).last_used_at is not None


def test_security_headers_and_request_id(client, tenant_a):
    r = upload(client, tenant_a, headers={"X-Request-ID": "trace-id-12345"})
    assert r.headers["X-Request-ID"] == "trace-id-12345" and r.headers["X-Content-Type-Options"] == "nosniff"
    r2 = upload(client, tenant_a, headers={"X-Request-ID": "bad id\r\ninjected"})
    assert r2.headers["X-Request-ID"].startswith("req_")
