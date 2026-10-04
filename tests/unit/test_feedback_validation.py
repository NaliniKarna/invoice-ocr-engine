from app.feedback.validation import validate_corrections

STORED = {"vendor": "Acme", "invoice_number": "A-1", "date": "2026-03-15", "currency": "NPR",
          "subtotal": "100.00", "tax": "13.00", "total": "113.00",
          "items": [{"description": "Pen", "quantity": "2", "unit_price": "50.00", "amount": "100.00"}]}


def v(*pairs):
    return validate_corrections(list(pairs), STORED)


def test_valid_corrections_capture_original_server_side():
    ok, err = v(("total", 123.5), ("vendor", "  Acme   Ltd "), ("date", "2026-03-16"))
    assert not err
    by = {c.field: c for c in ok}
    assert by["total"].original == "113.00" and by["total"].corrected == "123.50"
    assert by["vendor"].corrected == "Acme Ltd"


def test_unknown_field_rejected():
    _, err = v(("password", "x"), ("items[0].evil", 1), ("__class__", 1))
    assert {e["code"] for e in err} == {"unknown_field"} and len(err) == 3


def test_no_change_and_duplicates():
    _, err = v(("total", "113.00"), ("vendor", "New"), ("vendor", "Newer"))
    assert [e["code"] for e in err] == ["no_change", "duplicate_field"]


def test_invalid_values():
    _, err = v(("total", "abc"), ("date", "not-a-date"), ("currency", "XXX"), ("tax", True), ("vendor", "a" * 500))
    assert len(err) == 5 and all(e["code"] == "invalid_value" for e in err)


def test_item_index_bounds_and_append():
    ok, _ = v(("items[0].amount", 101), ("items[1].description", "New row"))
    assert len(ok) == 2
    _, err = v(("items[5].amount", 1))
    assert err[0]["code"] == "item_index_out_of_range"


def test_null_means_clear_field():
    ok, _ = v(("tax", None))
    assert ok[0].corrected is None
