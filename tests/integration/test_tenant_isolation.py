"""Security-critical: tenant A must never reach tenant B's process, result, batch or feedback."""
import io

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Feedback, OcrResult, Process
from tests.fakes import unique_png
from tests.helpers import submit_and_process, upload


@pytest.fixture
def b_proc(client, tenant_b, run_queue):
    return submit_and_process(client, tenant_b, run_queue)


def test_cannot_read_other_tenants_process(client, tenant_a, tenant_b, b_proc):
    assert client.get(f"/api/v1/bills/{b_proc}", headers=tenant_b.h).status_code == 200
    r = client.get(f"/api/v1/bills/{b_proc}", headers=tenant_a.h)
    assert r.status_code == 404                       # 404, not 403: existence is not revealed
    assert r.json() == client.get(f"/api/v1/bills/{'0'*20}", headers=tenant_a.h).json() | {"error": {**r.json()["error"]}}
    assert "vendor" not in r.text


def test_cannot_submit_feedback_for_other_tenants_process(client, tenant_a, tenant_b, b_proc, container):
    r = client.post(f"/api/v1/bills/{b_proc}/feedback", headers=tenant_a.h,
                    json={"corrections": [{"field": "total", "value": 1}]})
    assert r.status_code == 404
    with container.session_factory() as db:
        assert db.query(Feedback).count() == 0


def test_cannot_cancel_or_delete_other_tenants_process(client, tenant_a, tenant_b, queue):
    pid = upload(client, tenant_b).json()["process_id"]
    assert client.post(f"/api/v1/bills/{pid}/cancel", headers=tenant_a.h).status_code == 404
    assert client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 404
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_b.h).json()["status"] == "queued"


def test_cannot_read_other_tenants_batch(client, tenant_a, tenant_b):
    r = client.post("/api/v1/bills/bulk", files=[("files", ("a.png", unique_png(1), "image/png"))], headers=tenant_b.h)
    bid = r.json()["batch_id"]
    assert client.get(f"/api/v1/bills/batches/{bid}", headers=tenant_a.h).status_code == 404
    assert client.get(f"/api/v1/bills/batches/{bid}", headers=tenant_b.h).status_code == 200


def test_client_id_in_body_or_header_is_ignored(client, tenant_a, tenant_b, container):
    r = client.post("/api/v1/bills", files={"file": ("a.png", unique_png(2), "image/png")},
                    data={"metadata": '{"client_id": "%s"}' % tenant_b.client_id},
                    headers={**tenant_a.h, "X-Client-ID": tenant_b.client_id, "X-Tenant-ID": tenant_b.client_id})
    with container.session_factory() as db:
        assert db.get(Process, r.json()["process_id"]).client_id == tenant_a.client_id


def test_same_file_two_tenants_get_separate_objects(client, tenant_a, tenant_b, container):
    ida = upload(client, tenant_a, unique_png(3)).json()["process_id"]
    idb = upload(client, tenant_b, unique_png(3)).json()["process_id"]
    with container.session_factory() as db:
        ka, kb = db.get(Process, ida).storage_key, db.get(Process, idb).storage_key
    assert ka != kb and tenant_a.client_id in ka and tenant_b.client_id in kb


def test_idempotency_keys_are_tenant_scoped(client, tenant_a, tenant_b):
    h = {"Idempotency-Key": "shared-key-12345"}
    ra = upload(client, tenant_a, unique_png(4), headers=h)
    rb = upload(client, tenant_b, unique_png(4), headers=h)
    assert ra.json()["process_id"] != rb.json()["process_id"] and not rb.headers.get("Idempotent-Replay")


def test_db_composite_fk_rejects_cross_tenant_rows(container, tenant_a, tenant_b, client, b_proc):
    """Defence in depth: even buggy app code cannot attach A's feedback/result to B's process."""
    with container.session_factory() as db:
        res = db.query(OcrResult).filter_by(process_id=b_proc).one()
        db.add(Feedback(id="fb_x", submission_id="sub_x", client_id=tenant_a.client_id, process_id=b_proc,
                        field_path="total", original_value="1", corrected_value="2",
                        ocr_model_version_id=res.ocr_model_version_id,
                        extractor_model_version_id=res.extractor_model_version_id))
        with pytest.raises(IntegrityError):
            db.commit()


def test_repository_has_no_unscoped_lookup():
    from app.repositories.process import ProcessRepository
    import inspect
    sig = inspect.signature(ProcessRepository.get)
    assert list(sig.parameters)[:3] == ["self", "client_id", "process_id"]
