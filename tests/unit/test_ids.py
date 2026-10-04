import re

from app.core.ids import is_valid_id, new_id


def test_process_id_is_20_random_lowercase_alphanumerics():
    ids = {new_id("process") for _ in range(2000)}
    assert len(ids) == 2000
    assert all(re.fullmatch(r"[a-z0-9]{20}", i) for i in ids)
    assert all(is_valid_id("process", i) for i in ids)


def test_process_id_validation_rejects_other_shapes():
    for bad in ("", "a" * 19, "a" * 21, "A" * 20, "proc_" + "0" * 32, "../" + "a" * 17, "a" * 19 + "-"):
        assert not is_valid_id("process", bad)


def test_other_ids_keep_prefix():
    assert is_valid_id("batch", new_id("batch"))
    assert new_id("client").startswith("cli_")


# --- regressions found by hand-testing a real invoice image (Fictional Gulf Foods, AED) ---
def test_currency_detects_aed_and_labelled_currency():
    from app.extraction.normalize import detect_currency
    assert detect_currency("Subtotal AED 60.00") == "AED"
    assert detect_currency("Currency: AED\nTotal $5") == "AED"       # explicit label beats a symbol elsewhere
    assert detect_currency("Total CAD 10") == "CAD"
    assert detect_currency("we aed the cad") is None                  # lower-case words never match
    assert detect_currency("Total 10") is None


def test_invoice_number_does_not_capture_inside_the_word_invoice():
    from app.extraction.layout_rules import _INV
    from app.extraction.rules import _INV_RE
    for rx in (_INV, _INV_RE):
        assert rx.search("TAX INVOICE") is None
        assert rx.search("Invoice Date: 2026-08-01") is None or rx.search("Invoice Date: 2026-08-01").group(1) != "OICE"
        assert rx.search("Invoice: INV-2026-001").group(1) == "INV-2026-001"
    assert _INV.search("Invoice No: A-77/12").group(1) == "A-77/12"
    assert _INV.search("Bill to: Example Grocery Trading LLC") is None
