"""Schema v2 is additive: historical v1 results stay valid, clients that ignore new fields are unaffected, feedback accepts v2 fields."""
from app.models import Feedback, OcrResult
from tests.helpers import submit_and_process

V1_DATA = {"vendor": "Old Vendor", "invoice_number": "INV-1", "date": "2025-01-02", "currency": "NPR", "subtotal": "100.00",
           "tax": "13.00", "total": "113.00",
           "items": [{"description": "Thing", "quantity": "1", "unit_price": "100.00", "amount": "100.00"}]}


def test_historical_v1_result_is_still_served(client, container, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    with container.session_factory() as db:
        res = db.query(OcrResult).filter_by(process_id=pid).one()
        res.data, res.schema_version, res.meta = V1_DATA, "1", {}
        db.commit()
    r = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h)
    assert r.status_code == 200
    body = r.json()
    assert body["pipeline_versions"]["schema_version"] == "1" and body["result"]["vendor"] == "Old Vendor"
    assert body["result"]["total"] == 113.0 and body["result"]["items"][0]["description"] == "Thing"
    assert set(body["result"]) == {"vendor", "invoice_number", "date", "currency", "subtotal", "tax", "total", "items"}   # no v2 keys invented


def test_v1_keys_keep_their_meaning_in_v2_responses(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    res = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["result"]
    assert {"vendor", "invoice_number", "date", "currency", "subtotal", "tax", "total", "items"} <= set(res)
    assert res["total"] == 4746.0 and res["vendor"]


def test_feedback_accepts_v2_fields_and_line_item_extras(client, container, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    corr = [{"field": "document_number", "value": "DOC-9"}, {"field": "due_date", "value": "2026-04-15"},
            {"field": "discount", "value": 10}, {"field": "supplier_tax_id", "value": "457363381"},
            {"field": "items[0].unit", "value": "kg"}, {"field": "items[0].discount", "value": "1.5"}]
    r = client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": corr})
    assert r.status_code == 201 and r.json()["accepted"] == 6
    with container.session_factory() as db:
        rows = {f.field_path: f.corrected_value for f in db.query(Feedback)}
        assert rows["discount"] == "10.00" and rows["due_date"] == "2026-04-15" and rows["items[0].unit"] == "kg"


def test_feedback_still_rejects_unknown_fields_and_bad_values(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    for corr, code in (([{"field": "items[0].colour", "value": "red"}], "unknown_field"),
                       ([{"field": "due_date", "value": "not a date"}], "invalid_value"),
                       ([{"field": "discount", "value": "abc"}], "invalid_value")):
        r = client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": corr})
        assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == code


def test_openapi_documents_v2_fields_and_keeps_v1_paths(client):
    spec = client.get("/openapi.json").json()
    props = spec["components"]["schemas"]["ResultPayload"]["properties"]
    assert {"vendor", "total", "document_number", "due_date", "date_bs", "supplier_tax_id", "customer_name", "discount", "amount_due"} <= set(props)
    assert "/api/v1/bills" in spec["paths"] and "/api/v1/bills/{process_id}" in spec["paths"]
    assert spec["paths"]["/internal/v1/models"]["get"]["security"] == [{"AdminToken": []}]
    pv = spec["components"]["schemas"]["PipelineVersions"]["properties"]
    assert {"preprocessing", "schema_version", "normalization", "validation", "layout", "confidence_method"} <= set(pv)
