"""Additions for the invoice review web layer: document list, original-file download, and the `reviewed` flag that records
"a person checked this" (including "confirmed correct as extracted")."""
import pytest

from app.core.errors import Conflict
from app.ml.examples import ExampleService
from app.models import Feedback, Review
from tests.conftest import make_tenant
from tests.fakes import png_bytes
from tests.helpers import submit_and_process, upload


def fb(client, t, pid, corrections=(), **extra):
    body = {"corrections": [{"field": f, "value": v} for f, v in corrections], **extra}
    return client.post(f"/api/v1/bills/{pid}/feedback", headers=t.h, json=body)


# ---------------------------------------------------------------- list
def test_list_is_newest_first_with_headline_values_and_paginates(client, tenant_a, run_queue):
    ids = [submit_and_process(client, tenant_a, run_queue, png_bytes()) for _ in range(3)]
    r = client.get("/api/v1/bills", headers=tenant_a.h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3 and {i["process_id"] for i in items} == set(ids)
    first = items[0]
    assert first["status"] == "completed" and first["vendor"] == "Himalayan Traders Pvt Ltd" and first["total"] == 4746.0
    assert first["reviewed"] is False and "next_cursor" not in r.json()

    p1 = client.get("/api/v1/bills?limit=2", headers=tenant_a.h).json()
    assert len(p1["items"]) == 2 and p1["next_cursor"]
    p2 = client.get("/api/v1/bills", params={"limit": 2, "cursor": p1["next_cursor"]}, headers=tenant_a.h).json()
    assert len(p2["items"]) == 1 and "next_cursor" not in p2
    assert {i["process_id"] for i in p1["items"] + p2["items"]} == set(ids)


def test_list_shows_documents_still_being_read_without_values(client, tenant_a):
    assert upload(client, tenant_a).status_code == 202            # not processed: still queued
    item = client.get("/api/v1/bills", headers=tenant_a.h).json()["items"][0]
    assert item["status"] == "queued" and "vendor" not in item and item["filename"] == "bill.png"


def test_list_is_tenant_scoped(client, tenant_a, tenant_b, run_queue):
    submit_and_process(client, tenant_a, run_queue)
    assert client.get("/api/v1/bills", headers=tenant_b.h).json()["items"] == []


def test_list_validates_parameters_and_needs_read_scope(client, container, tenant_a):
    assert client.get("/api/v1/bills?limit=0", headers=tenant_a.h).status_code == 422
    assert client.get("/api/v1/bills?limit=101", headers=tenant_a.h).status_code == 422
    assert client.get("/api/v1/bills?review=maybe", headers=tenant_a.h).status_code == 422
    assert client.get("/api/v1/bills?cursor=not-a-cursor", headers=tenant_a.h).status_code == 422
    assert client.get("/api/v1/bills").status_code == 401
    t = make_tenant(container, "writeonly", scopes=["bills:write"])
    assert client.get("/api/v1/bills", headers=t.h).status_code == 403


def test_review_filter_pending_and_reviewed(client, tenant_a, run_queue):
    a = submit_and_process(client, tenant_a, run_queue)
    b = submit_and_process(client, tenant_a, run_queue)
    upload(client, tenant_a)                                      # queued: never "pending review"
    assert fb(client, tenant_a, a, reviewed=True).status_code == 201

    def ids(q):
        return {i["process_id"] for i in client.get(f"/api/v1/bills?review={q}", headers=tenant_a.h).json()["items"]}
    assert ids("pending") == {b} and ids("reviewed") == {a}
    item = next(i for i in client.get("/api/v1/bills", headers=tenant_a.h).json()["items"] if i["process_id"] == a)
    assert item["reviewed"] is True and item["reviewed_at"]


# ---------------------------------------------------------------- original file
def test_file_returns_the_original_with_safe_headers(client, tenant_a, run_queue):
    data = png_bytes()
    pid = submit_and_process(client, tenant_a, run_queue, data)
    r = client.get(f"/api/v1/bills/{pid}/file", headers=tenant_a.h)
    assert r.status_code == 200 and r.content == data and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and "no-store" in r.headers["cache-control"]
    assert r.headers["content-disposition"].startswith("inline")


def test_file_is_tenant_scoped_and_needs_read_scope(client, container, tenant_a, tenant_b, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert client.get(f"/api/v1/bills/{pid}/file", headers=tenant_b.h).status_code == 404
    assert client.get(f"/api/v1/bills/{pid}/file").status_code == 401
    t = make_tenant(container, "writeonly2", scopes=["bills:write"])
    assert client.get(f"/api/v1/bills/{pid}/file", headers=t.h).status_code == 403
    assert client.get("/api/v1/bills/not-an-id/file", headers=tenant_a.h).status_code == 404


def test_file_is_gone_after_delete(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 204
    assert client.get(f"/api/v1/bills/{pid}/file", headers=tenant_a.h).status_code == 404
    assert client.get("/api/v1/bills", headers=tenant_a.h).json()["items"] == []


# ---------------------------------------------------------------- reviewed flag
def test_confirming_without_changes_is_recorded_as_reviewed(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = fb(client, tenant_a, pid, reviewed=True)
    assert r.status_code == 201 and r.json()["accepted"] == 0 and r.json()["reviewed"] is True
    with container.session_factory() as db:
        rev = db.query(Review).one()
        assert (rev.client_id, rev.process_id, rev.outcome) == (tenant_a.client_id, pid, "confirmed")
        assert db.query(Feedback).count() == 0
    # the stored machine result is untouched
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["result"]["total"] == 4746.0


def test_reviewing_with_corrections_stores_both(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = fb(client, tenant_a, pid, [("vendor", "Himalayan Traders")], reviewed=True, comment="logo only")
    assert r.status_code == 201 and r.json()["accepted"] == 1
    with container.session_factory() as db:
        rev = db.query(Review).one()
        assert rev.outcome == "corrected" and rev.submission_id == r.json()["submission_id"]
        assert db.query(Feedback).one().field_path == "vendor"


def test_a_document_can_be_reviewed_only_once(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert fb(client, tenant_a, pid, reviewed=True).status_code == 201
    again = fb(client, tenant_a, pid, [("vendor", "X Ltd")], reviewed=True)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_reviewed"
    with container.session_factory() as db:
        assert db.query(Review).count() == 1 and db.query(Feedback).count() == 0   # the rejected attempt stored nothing


def test_invalid_corrections_with_reviewed_store_nothing(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = fb(client, tenant_a, pid, [("total", "not a number")], reviewed=True)
    assert r.status_code == 422
    with container.session_factory() as db:
        assert db.query(Review).count() == 0 and db.query(Feedback).count() == 0


def test_empty_submission_without_reviewed_is_still_rejected(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert fb(client, tenant_a, pid).status_code == 422
    assert fb(client, tenant_a, pid, reviewed=False).status_code == 422


def test_legacy_corrections_without_reviewed_create_no_review(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = fb(client, tenant_a, pid, [("total", 4800.5)])
    assert r.status_code == 201 and r.json()["reviewed"] is False
    with container.session_factory() as db:
        assert db.query(Review).count() == 0
    assert fb(client, tenant_a, pid, [("vendor", "Himalayan Traders")]).status_code == 201   # repeatable, as before


def test_other_tenants_cannot_review(client, tenant_a, tenant_b, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert fb(client, tenant_b, pid, reviewed=True).status_code == 404


def test_deleting_a_document_removes_its_review_unless_pooled(client, container, run_queue):
    t = make_tenant(container, "plain")
    pid = submit_and_process(client, t, run_queue)
    assert fb(client, t, pid, reviewed=True).status_code == 201
    assert client.delete(f"/api/v1/bills/{pid}", headers=t.h).status_code == 204
    with container.session_factory() as db:
        assert db.query(Review).count() == 0


# ---------------------------------------------------------------- pipeline: a human confirmation can become an example
def _policy(container, t, pol):
    with container.session_factory() as db:
        ExampleService(db, container.settings).set_policy(t.client_id, pol, by="test")
        db.commit()


def test_confirmed_document_can_be_proposed_as_an_example(client, container, run_queue):
    t = make_tenant(container, "confirmer")
    _policy(container, t, "tenant_only")
    pid = submit_and_process(client, t, run_queue)
    assert fb(client, t, pid, reviewed=True).status_code == 201
    with container.session_factory() as db:
        ex = ExampleService(db, container.settings).propose(t.client_id, pid)
        db.commit()
        assert ex.source == "client_confirmation" and ex.truth["vendor"] == "Himalayan Traders Pvt Ltd"
        assert ex.truth["total"] == "4746.00" and ex.status in ("proposed", "needs_review")   # still goes through gates and review


def test_unreviewed_machine_result_is_still_not_ground_truth(client, container, run_queue):
    t = make_tenant(container, "unreviewed")
    _policy(container, t, "tenant_only")
    pid = submit_and_process(client, t, run_queue)
    with container.session_factory() as db:
        with pytest.raises(Conflict) as e:
            ExampleService(db, container.settings).propose(t.client_id, pid)
        assert e.value.code == "no_corrections"
