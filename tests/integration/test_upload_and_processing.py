import re
import json

from app.models import Process
from tests.fakes import jpeg_bytes, pdf_bytes, png_bytes
from tests.helpers import submit_and_process, upload


def test_submit_returns_202_immediately_and_queues(client, tenant_a, queue, container):
    r = upload(client, tenant_a, meta=json.dumps({"order": "A-1"}))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and re.fullmatch(r"[a-z0-9]{20}", body["process_id"])
    assert r.headers["Location"] == f"/api/v1/bills/{body['process_id']}"
    assert queue.items == [body["process_id"]]
    with container.session_factory() as db:
        p = db.get(Process, body["process_id"])
        assert p.client_id == tenant_a.client_id and p.status == "queued" and p.enqueued_at is not None
        assert p.storage_key.startswith(f"tenants/{tenant_a.client_id}/documents/{p.id}/")


def test_full_flow_completed_result(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "completed"
    res = body["result"]
    assert res["vendor"] == "Himalayan Traders Pvt Ltd" and res["invoice_number"] == "INV-2026-0042"
    assert res["date"] == "2026-03-15" and res["currency"] == "NPR"
    assert (res["subtotal"], res["tax"], res["total"]) == (4200.0, 546.0, 4746.0)
    assert len(res["items"]) == 2
    assert body["model_versions"] == {"ocr": "fake-ocr@1.0", "extractor": "rule-based-baseline@0.1.0"}
    assert 0 < body["confidence"] <= 1 and body["requires_review"] is False


def test_status_before_processing_is_queued(client, tenant_a):
    pid = upload(client, tenant_a).json()["process_id"]
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["status"] == "queued" and "result" not in body


def test_accepts_jpeg_and_pdf(client, tenant_a, run_queue):
    for data, name, ct in ((jpeg_bytes(), "a.jpg", "image/jpeg"), (pdf_bytes(), "a.pdf", "application/pdf")):
        assert upload(client, tenant_a, data, name, ct).status_code == 202
    assert all(r.outcome.value == "completed" for r in run_queue())


def test_rejects_bad_files_with_safe_errors(client, tenant_a, queue):
    cases = [(b"MZ" + b"\0" * 50, 415, "unsupported_file_type"), (b"", 422, "empty_file"),
             (png_bytes()[:30], 422, "invalid_file"), (b"x" * (300 * 1024), 413, "file_too_large")]
    t = tenant_a
    for data, status, code in cases:
        # tenant_a is enterprise (10MB): use a free-plan tenant for the size case
        pass
    for data, status, code in cases[:3]:
        r = upload(client, t, data)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    assert queue.items == []


def test_oversize_uses_plan_limit(client, container, queue):
    from tests.conftest import make_tenant
    free = make_tenant(container, "free1", plan="free")
    r = upload(client, free, b"\x89PNG\r\n\x1a\n" + b"0" * (250 * 1024))
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"


def test_content_length_guard(client, tenant_a):
    r = client.post("/api/v1/bills", content=b"x", headers={**tenant_a.h, "Content-Length": str(10**10),
                                                             "Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413


def test_bad_metadata_rejected(client, tenant_a):
    for bad in ("not json", json.dumps([1]), json.dumps({"a": {"nested": 1}}), json.dumps({"k": "v" * 600})):
        assert upload(client, tenant_a, meta=bad).status_code == 422


def test_malicious_filename_not_used_in_storage_path(client, tenant_a, container):
    r = upload(client, tenant_a, name="../../../etc/passwd.png")
    assert r.status_code == 202
    with container.session_factory() as db:
        p = db.get(Process, r.json()["process_id"])
        assert ".." not in p.storage_key and p.original_filename == "passwd.png"


def test_cancel_queued_then_worker_skips(client, tenant_a, queue, run_queue):
    pid = upload(client, tenant_a).json()["process_id"]
    r = client.post(f"/api/v1/bills/{pid}/cancel", headers=tenant_a.h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert [x.outcome.value for x in run_queue()] == ["skipped"]
    assert client.post(f"/api/v1/bills/{pid}/cancel", headers=tenant_a.h).status_code == 409


def test_cannot_cancel_completed(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert client.post(f"/api/v1/bills/{pid}/cancel", headers=tenant_a.h).status_code == 409


def test_delete_purges_document_and_result(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    from app.models import OcrResult
    with container.session_factory() as db:
        key = db.get(Process, pid).storage_key
    assert client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 204
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 404
    import pytest
    with pytest.raises(Exception):
        container.storage.get(key)
    with container.session_factory() as db:
        assert db.query(OcrResult).filter_by(process_id=pid).one().data is None


def test_malformed_ids_404(client, tenant_a):
    for bad in ("1", "proc_xyz", "G" * 20, "../../x"):
        assert client.get(f"/api/v1/bills/{bad}", headers=tenant_a.h).status_code in (404,)


def test_unhandled_error_is_generic(container, tenant_a, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.services.processes import ProcessService
    monkeypatch.setattr(ProcessService, "get_detail", lambda *a, **k: 1 / 0)
    c = TestClient(create_app(container), raise_server_exceptions=False)
    r = c.get(f"/api/v1/bills/{'0'*20}", headers=tenant_a.h)
    assert r.status_code == 500 and "division" not in r.text and "Traceback" not in r.text
    assert r.json()["error"]["request_id"]


def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
