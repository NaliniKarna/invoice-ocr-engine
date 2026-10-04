from datetime import date
from decimal import Decimal

from app.extraction.normalize import parse_date, parse_money
from app.extraction.rules import RuleBasedExtractor
from app.extraction.postprocess import build_output
from app.ocr.base import OCRLine, OCRPage, OCRResult
from tests.fakes import INVOICE_LINES


def ocr_of(lines, conf=0.95):
    return OCRResult("t", "1", [OCRPage(1, 1, 1, [OCRLine(t, conf, (0, 0, 1, 1)) for t in lines])])


def run(lines, **kw):
    raw = RuleBasedExtractor().extract(ocr_of(lines))
    return build_output(raw, day_first=True, review_threshold=0.8, today=date(2026, 9, 1), **kw)


def test_money_parsing():
    assert parse_money("1,234.50") == Decimal("1234.50")
    assert parse_money("1.234,50") == Decimal("1234.50")
    assert parse_money("1,23,456.00") == Decimal("123456.00")
    assert parse_money("NPR 4,746.00") == Decimal("4746.00")
    assert parse_money("abc") is None and parse_money(None) is None


def test_date_parsing_and_ambiguity():
    assert parse_date("15/03/2026") == (date(2026, 3, 15), False)
    assert parse_date("03/04/2026") == (date(2026, 4, 3), True)
    assert parse_date("2026-03-15") == (date(2026, 3, 15), False)
    assert parse_date("15 Mar 2026")[0] == date(2026, 3, 15)
    assert parse_date("31/02/2026")[0] is None


def test_full_invoice():
    o = run(INVOICE_LINES)
    d = o.data
    assert (d.vendor, d.invoice_number, d.date, d.currency) == ("Himalayan Traders Pvt Ltd", "INV-2026-0042", date(2026, 3, 15), "NPR")
    assert (d.subtotal, d.tax, d.total) == (Decimal("4200.00"), Decimal("546.00"), Decimal("4746.00"))
    assert len(d.items) == 2 and d.items[0].amount == Decimal("3000.00")
    assert not [i for i in o.issues if i.severity == "error"]
    assert o.confidence >= 0.5


def test_totals_mismatch_is_flagged_for_review():
    lines = [l.replace("4,746.00", "9,999.00") for l in INVOICE_LINES]
    o = run(lines)
    assert any(i.code == "totals_mismatch" for i in o.issues) and o.requires_review


def test_empty_ocr_requires_review_and_zero_confidence():
    o = run([])
    assert o.requires_review and o.confidence == 0.0
    assert any(i.code == "missing_total" for i in o.issues)


def test_future_date_flagged():
    o = run(["Shop", "Invoice No: A-1", "Date: 01/01/2031", "Total 10.00"])
    assert any(i.code == "date_out_of_range" for i in o.issues)
