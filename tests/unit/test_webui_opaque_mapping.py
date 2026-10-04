import pytest

from app.webui import mapping
from app.webui.opaque import Opaque, OpaqueError


def test_opaque_round_trip_and_hides_the_value():
    o = Opaque("k" * 32)
    tok = o.seal("abcdefghij0123456789")
    assert "abcdefghij" not in tok and o.open(tok) == "abcdefghij0123456789"
    assert o.seal("x") == o.seal("x")                       # stable, so the browser can keep using a link


def test_opaque_rejects_tampering_and_other_keys():
    o = Opaque("k" * 32)
    tok = o.seal("abcdefghij0123456789")
    for bad in ("", "x", tok[:-2] + ("AA" if not tok.endswith("AA") else "BB"), tok + "A", "!!!"):
        with pytest.raises(OpaqueError):
            o.open(bad)
    with pytest.raises(OpaqueError):
        Opaque("z" * 32).open(tok)


def test_states():
    assert mapping.ui_state("queued", False) == "reading" and mapping.ui_state("processing", False) == "reading"
    assert mapping.ui_state("completed", False) == "ready" and mapping.ui_state("completed", True) == "reviewed"
    assert mapping.ui_state("failed", False) == "failed"


def test_flags_use_plain_language_only():
    out = mapping.flags([{"code": "totals_mismatch", "field": "total", "severity": "warning", "message": "internal text"},
                         {"code": "never_heard_of_it", "field": "tax"}, {"code": "never_heard_of_it"}])
    assert out == {"total": "The amounts don't add up to the total.", "tax": mapping.GENERIC_CHECK}


def test_build_corrections_only_real_differences():
    orig = {"vendor": "Acme  Ltd", "total": 4746.0, "date": "2026-01-02", "items": [{"description": "Tea", "amount": 10}]}
    sub = {"values": {"vendor": "Acme Ltd", "total": "4,746.00", "date": "2026-01-02", "tax": ""},
           "items": [{"description": "Tea", "amount": "10.00"}]}
    assert mapping.build_corrections(orig, sub).corrections == []
    sub["values"].update(vendor="Acme Traders", total="4800", tax="5")
    got = {c["field"]: c["value"] for c in mapping.build_corrections(orig, sub).corrections}
    assert got == {"vendor": "Acme Traders", "total": "4800.00", "tax": "5.00"}


def test_build_corrections_clear_errors_and_new_rows():
    orig = {"vendor": "A", "items": [{"description": "Tea"}]}
    b = mapping.build_corrections(orig, {"values": {"vendor": "", "total": "abc", "date": "31/12/2026"}, "items": []})
    assert b.corrections == [{"field": "vendor", "value": None}]
    assert b.errors == {"total": "Enter a number.", "date": "Enter a valid date."}
    b = mapping.build_corrections(orig, {"values": {}, "items": [{"description": "Tea"}, {"description": "Milk", "amount": "3"}]})
    assert b.corrections == [{"field": "items[1].description", "value": "Milk"}, {"field": "items[1].amount", "value": "3.00"}]
    b = mapping.build_corrections(orig, {"values": {}, "items": [{}, {}, {}]})
    assert "items" in b.errors
