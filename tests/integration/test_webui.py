"""The invoice review web layer, run against the real API (the backend TestClient is the BFF's HTTP client)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models import Feedback, Review
from app.webui.main import create_app as create_ui
from app.webui.settings import UISettings
from tests.conftest import make_tenant
from tests.fakes import png_bytes
from tests.helpers import submit_and_process

KEY_HOLDER = {}


@pytest.fixture
def collector(container):
    t = make_tenant(container, "collection")
    KEY_HOLDER["t"] = t
    return t


@pytest.fixture
def ui(client, collector):
    st = UISettings(api_key=collector.api_key, api_base_url="http://testserver", max_upload_bytes=200_000, max_files_per_upload=3)
    return TestClient(create_ui(st, http=client), raise_server_exceptions=False)


def _upload(ui, *files):
    return ui.post("/ui/upload", files=[("files", f) for f in files])


def _ready(ui, client, collector, run_queue):
    r = _upload(ui, ("a.png", png_bytes(), "image/png"))
    assert r.status_code == 200 and r.json()["results"] == [{"name": "a.png", "ok": True}]
    run_queue()
    items = ui.get("/ui/invoices").json()["items"]
    return items[0]["id"]


# ------------------------------------------------------------------ nothing internal reaches the browser
def test_key_and_ids_never_reach_the_browser(ui, client, collector, run_queue):
    token = _ready(ui, client, collector, run_queue)
    seen = [ui.get("/").text, ui.get("/ui/config").text, ui.get("/ui/invoices").text, ui.get(f"/ui/invoices/{token}").text]
    for p in (Path("app/webui/static")).iterdir():
        seen.append(p.read_text())
    pid = client.get("/api/v1/bills", headers=collector.h).json()["items"][0]["process_id"]
    blob = "\n".join(seen)
    assert collector.api_key not in blob and collector.client_id not in blob and pid not in blob
    for word in ("X-API-Key", "process_id", "confidence", "requires_review", "swagger", "openapi"):
        assert word.lower() not in blob.lower()
    r = ui.get("/")
    assert "x-api-key" not in {k.lower() for k in r.headers} and not r.cookies


def test_no_swagger_or_schema(ui):
    for p in ("/docs", "/redoc", "/openapi.json", "/api/v1/bills"):
        assert ui.get(p).status_code in (404, 405)


def test_security_headers(ui):
    r = ui.get("/")
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff" and "no-store" in r.headers["cache-control"]


def test_cross_origin_posts_are_refused(ui):
    f = [("files", ("a.png", png_bytes(), "image/png"))]
    assert ui.post("/ui/upload", files=f, headers={"Origin": "https://evil.example"}).status_code == 403
    assert ui.post("/ui/upload", files=f, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert ui.post("/ui/upload", files=f, headers={"Origin": "http://testserver"}).status_code == 200


# ------------------------------------------------------------------ upload and list
def test_upload_validates_type_and_size(ui):
    r = _upload(ui, ("note.png", b"hello world, not an image", "image/png"), ("big.png", png_bytes() + b"0" * 250_000, "image/png"),
                ("ok.png", png_bytes(), "image/png")).json()["results"]
    assert [x["ok"] for x in r] == [False, False, True]
    assert r[0]["message"].startswith("This file type isn't supported") and r[1]["message"] == "This file is too large."
    assert _upload(ui, *[(f"{i}.png", png_bytes(), "image/png") for i in range(4)]).status_code == 400


def test_list_states_reading_then_ready_then_reviewed(ui, client, collector, run_queue):
    _upload(ui, ("a.png", png_bytes(), "image/png"))
    it = ui.get("/ui/invoices").json()["items"][0]
    assert it["state"] == "reading" and it["name"] == "a.png" and set(it) == {"id", "name", "state", "vendor", "invoice_number", "date", "total", "currency"}
    run_queue()
    it = ui.get("/ui/invoices").json()["items"][0]
    assert it["state"] == "ready" and it["vendor"] == "Himalayan Traders Pvt Ltd" and it["total"] == "4746.00"
    assert ui.post(f"/ui/invoices/{it['id']}/submit", json={}).status_code == 200
    assert ui.get("/ui/invoices").json()["items"][0]["state"] == "reviewed"


def test_bad_cursor_and_tokens(ui):
    assert ui.get("/ui/invoices?cursor=junk").status_code == 400
    for p in ("/ui/invoices/junk", "/ui/invoices/junk/file"):
        assert ui.get(p).status_code == 404
    assert ui.post("/ui/invoices/junk/submit", json={}).status_code == 404


# ------------------------------------------------------------------ review
def test_detail_has_values_items_flags_and_no_internals(ui, client, collector, run_queue):
    token = _ready(ui, client, collector, run_queue)
    d = ui.get(f"/ui/invoices/{token}").json()
    assert d["state"] == "ready" and d["values"]["vendor"] == "Himalayan Traders Pvt Ltd" and d["values"]["total"] == "4746.00"
    assert isinstance(d["items"], list) and isinstance(d["flags"], dict)
    text = json.dumps(d).lower()
    for word in ("process", "confidence", "requires_review", "engine", "attempt", "model_version", "client_id"):
        assert word not in text


def test_original_is_served_through_the_ui(ui, client, collector, run_queue):
    token = _ready(ui, client, collector, run_queue)
    r = ui.get(f"/ui/invoices/{token}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == png_bytes()
    assert "no-store" in r.headers["cache-control"]


def test_submit_unchanged_records_a_confirmation(ui, client, collector, run_queue, container):
    token = _ready(ui, client, collector, run_queue)
    r = ui.post(f"/ui/invoices/{token}/submit", json={"values": ui.get(f"/ui/invoices/{token}").json()["values"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    with container.session_factory() as db:
        assert db.query(Review).one().outcome == "confirmed" and db.query(Feedback).count() == 0


def test_submit_with_corrections_saves_feedback_and_original_stays(ui, client, collector, run_queue, container):
    token = _ready(ui, client, collector, run_queue)
    d = ui.get(f"/ui/invoices/{token}").json()
    vals = dict(d["values"], vendor="Himalayan Traders", total="4800.50")
    r = ui.post(f"/ui/invoices/{token}/submit", json={"values": vals, "note": "logo only"})
    assert r.status_code == 200
    with container.session_factory() as db:
        assert db.query(Review).one().outcome == "corrected"
        assert {f.field_path for f in db.query(Feedback)} == {"vendor", "total"}
    pid = client.get("/api/v1/bills", headers=collector.h).json()["items"][0]["process_id"]
    assert client.get(f"/api/v1/bills/{pid}", headers=collector.h).json()["result"]["vendor"] == "Himalayan Traders Pvt Ltd"
    after = ui.get(f"/ui/invoices/{token}").json()
    assert after["state"] == "reviewed" and after["values"]["vendor"] == "Himalayan Traders Pvt Ltd"


def test_invalid_value_gives_a_field_message_and_stores_nothing(ui, client, collector, run_queue, container):
    token = _ready(ui, client, collector, run_queue)
    r = ui.post(f"/ui/invoices/{token}/submit", json={"values": {"total": "lots"}})
    assert r.status_code == 422 and r.json()["fields"] == {"total": "Enter a number."} and r.json()["message"] == "Your changes couldn't be submitted."
    with container.session_factory() as db:
        assert db.query(Review).count() == 0


def test_second_submit_is_a_friendly_conflict(ui, client, collector, run_queue):
    token = _ready(ui, client, collector, run_queue)
    assert ui.post(f"/ui/invoices/{token}/submit", json={}).status_code == 200
    r = ui.post(f"/ui/invoices/{token}/submit", json={"values": {"vendor": "Other"}})
    assert r.status_code == 409 and r.json() == {"message": "This invoice was already reviewed."}


def test_not_ready_cannot_be_submitted(ui):
    _upload(ui, ("a.png", png_bytes(), "image/png"))
    token = ui.get("/ui/invoices").json()["items"][0]["id"]
    assert ui.post(f"/ui/invoices/{token}/submit", json={}).status_code == 409


def test_next_invoice_is_the_oldest_waiting(ui, client, collector, run_queue):
    for n in range(2):
        _upload(ui, (f"{n}.png", png_bytes(), "image/png"))
    run_queue()
    items = ui.get("/ui/invoices").json()["items"]
    first, second = items[1]["id"], items[0]["id"]            # list is newest first
    r = ui.post(f"/ui/invoices/{first}/submit", json={}).json()
    assert r["next"] == second
    assert ui.post(f"/ui/invoices/{second}/submit", json={}).json()["next"] is None


def test_wrong_key_shows_a_generic_message_without_details(client, collector):
    st = UISettings(api_key="w" * 32, api_base_url="http://testserver")
    bad = TestClient(create_ui(st, http=client), raise_server_exceptions=False)
    r = bad.get("/ui/invoices")
    assert r.status_code == 503 and "key" not in r.text.lower() and "401" not in r.text


def test_other_tenants_invoices_are_not_reachable(ui, client, tenant_b, run_queue):
    pid = submit_and_process(client, tenant_b, run_queue)
    from app.webui.opaque import Opaque
    token = Opaque(KEY_HOLDER["t"].api_key).seal(pid)
    assert ui.get(f"/ui/invoices/{token}").status_code == 404
    assert ui.get(f"/ui/invoices/{token}/file").status_code == 404
