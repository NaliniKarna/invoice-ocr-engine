"""Pipeline: page-level failures, extractor fallback, strict handling of untrusted extractor output, layout and evidence."""
from datetime import date
from decimal import Decimal

import pytest
from PIL import Image

from app.extraction.base import Extractor, RawExtraction, RawField, RawItem
from app.extraction.layout_rules import LayoutRulesExtractor
from app.extraction.pipeline import DocumentPipeline
from app.extraction.rules import RuleBasedExtractor
from app.layout.heuristic import HeuristicLayoutAnalyzer
from app.ocr.base import ExtractionError, OCRLine, OCRPage, OCRPermanentError, OCRProvider, OCRResult, OCRTransientError, OCRWord


def line(text, x0, y0, x1=None, y1=None, conf=0.95):
    x1 = x1 if x1 is not None else x0 + 12 * len(text)
    y1 = y1 if y1 is not None else y0 + 28
    # words laid out left to right inside the line box
    words, x = [], x0
    for w in text.split(" "):
        words.append(OCRWord(w, conf, (x, y0, x + 12 * len(w), y1))); x += 12 * (len(w) + 1)
    return OCRLine(text, conf, (x0, y0, x1, y1), words)


def cells(row_y, *pairs):
    return [line(t, x, row_y) for x, t in pairs]


INVOICE = [
    line("Himal Stationers", 100, 50), line("Tel: 01-4511345", 100, 90), line("PAN No: 457363381", 100, 130),
    line("INVOICE", 100, 180), line("Invoice No: INV-2026-0042", 100, 220), line("Date: 15/03/2026", 100, 260),
    line("Bill To:", 100, 300), line("Rara Lake Resort", 100, 330),
    *cells(400, (100, "Description"), (600, "Qty"), (750, "Rate"), (950, "Amount")),
    *cells(440, (100, "Rice 25kg"), (600, "2"), (750, "1,500.00"), (950, "3,000.00")),
    *cells(480, (100, "Cooking Oil"), (600, "1"), (750, "1,200.00"), (950, "1,200.00")),
    *cells(560, (700, "Sub Total"), (950, "4,200.00")),
    *cells(600, (700, "VAT 13%"), (950, "546.00")),
    *cells(640, (700, "Grand Total"), (950, "4,746.00")),
]


# The same document as a line-oriented engine (Tesseract) returns it: one OCRLine per text row.
ROW_LINES = [l for l in INVOICE if l.bbox[1] < 400] + [
    line("Description Qty Rate Amount", 100, 400), line("Rice 25kg 2 1,500.00 3,000.00", 100, 440),
    line("Cooking Oil 1 1,200.00 1,200.00", 100, 480), line("Sub Total 4,200.00", 700, 560),
    line("VAT 13% 546.00", 700, 600), line("Grand Total 4,746.00", 700, 640)]


def ocr_result(lines_by_page):
    return OCRResult("fake", "1", [OCRPage(i, 1240, 1754, ls) for i, ls in enumerate(lines_by_page, 1)])


class FakeOCR(OCRProvider):
    name, version = "fake-ocr", "1"

    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, 0

    def recognize(self, pages):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def img():
    return Image.new("RGB", (10, 10), "white")


def pipeline(ocr, extractor, **kw):
    return DocumentPipeline(ocr, extractor, day_first=True, review_threshold=0.8, **kw)


# ------------------------------------------------------------------ layout
def test_layout_finds_table_columns_and_totals_region():
    lay = HeuristicLayoutAnalyzer().analyze(ocr_result([INVOICE]))
    pg = lay.pages[0]
    assert len(pg.tables) == 1
    t = pg.tables[0]
    assert [c.name for c in t.columns] == ["description", "quantity", "unit_price", "amount"] and len(t.rows) == 2
    assert any("Grand Total" in r.text for r in pg.rows)


# ------------------------------------------------------------------ layout-rules extractor
def test_layout_extractor_reads_header_customer_items_and_totals_with_evidence():
    out = pipeline(FakeOCR(ocr_result([INVOICE])), LayoutRulesExtractor()).run([img()])
    d = out.extraction.data
    assert (d.vendor, d.invoice_number, d.date) == ("Himal Stationers", "INV-2026-0042", date(2026, 3, 15))
    assert (d.subtotal, d.tax, d.total) == (Decimal("4200.00"), Decimal("546.00"), Decimal("4746.00"))
    assert d.customer_name == "Rara Lake Resort" and d.supplier_tax_id == "457363381"
    assert [i.description for i in d.items] == ["Rice 25kg", "Cooking Oil"]
    assert d.items[0].quantity == Decimal("2") and d.items[0].unit_price == Decimal("1500.00") and d.items[0].amount == Decimal("3000.00")
    assert out.extraction.issues == [] or all(i.severity != "error" for i in out.extraction.issues)
    ev = out.extraction.evidence["total"]
    assert ev.page == 1 and ev.bbox and "4,746.00" in (ev.source_text or "") and ev.raw == "4,746.00" and ev.method.startswith("layout")
    assert out.extractor.name == "layout-rules" and out.fallback_from is None


def test_reference_extractor_still_works_on_the_same_ocr():
    out = pipeline(FakeOCR(ocr_result([ROW_LINES])), RuleBasedExtractor()).run([img()])
    assert out.extraction.data.total == Decimal("4746.00") and out.extractor.name == "rule-based-baseline"


def test_bs_date_with_miti_label_is_converted_and_original_kept():
    lines = [line("Kathmandu Traders", 100, 50), line("INVOICE", 100, 100), line("Invoice No: A-77", 100, 140),
             line("Miti: 2082/06/15", 100, 180), *cells(560, (700, "Grand Total"), (950, "100.00"))]
    d = pipeline(FakeOCR(ocr_result([lines])), LayoutRulesExtractor()).run([img()]).extraction.data
    assert d.date == date(2025, 10, 1) and d.date_bs == "2082-06-15"


# ------------------------------------------------------------------ multi-page & failures
def test_multi_page_table_continues_and_items_keep_page_numbers():
    p2 = [*cells(100, (100, "Description"), (600, "Qty"), (750, "Rate"), (950, "Amount")),
          *cells(140, (100, "Sugar 10kg"), (600, "5"), (750, "1,800.00"), (950, "9,000.00")),
          *cells(300, (700, "Grand Total"), (950, "13,200.00"))]
    p1 = [l for l in INVOICE if "Grand Total" not in l.text and l.bbox[1] < 540]
    out = pipeline(FakeOCR(ocr_result([p1, p2])), LayoutRulesExtractor()).run([img(), img()])
    d = out.extraction.data
    assert [i.description for i in d.items] == ["Rice 25kg", "Cooking Oil", "Sugar 10kg"] and d.total == Decimal("13200.00")
    assert out.ocr.pages[1].number == 2


def test_failed_render_page_is_recorded_not_dropped_and_forces_review():
    out = pipeline(FakeOCR(ocr_result([INVOICE])), LayoutRulesExtractor()).run([img(), None])
    assert out.failed_pages == [2] and out.extraction.requires_review
    assert any(i.code == "page_ocr_failed" and i.severity == "error" for i in out.extraction.issues)
    assert [p.status for p in out.ocr.pages] == ["ok", "failed"] and out.ocr.pages[1].error_code == "page_render_failed"


def test_fail_on_partial_policy_fails_the_job():
    with pytest.raises(OCRPermanentError):
        pipeline(FakeOCR(ocr_result([INVOICE])), LayoutRulesExtractor(), fail_on_partial=True).run([img(), None])


def test_ocr_errors_propagate_with_their_retry_class():
    with pytest.raises(OCRTransientError):
        pipeline(FakeOCR(exc=OCRTransientError("x")), RuleBasedExtractor()).run([img()])


# ------------------------------------------------------------------ untrusted extractor output
class Broken(Extractor):
    name, version = "broken", "0"

    def __init__(self, mode):
        self.mode = mode

    def extract(self, ocr, pages=None):
        if self.mode == "crash":
            raise RuntimeError("model exploded at /srv/models/secret")
        if self.mode == "type":
            return {"total": "1"}                                         # not a RawExtraction
        if self.mode == "currency":
            return RawExtraction({"total": RawField("10", 0.9), "currency": RawField("$$$$", 0.9)})
        if self.mode == "garbage_amount":
            return RawExtraction({"total": RawField("ten dollars", 0.9)})
        if self.mode == "huge":
            return RawExtraction({"total": RawField("9" * 30, 0.9)})
        raise AssertionError


@pytest.mark.parametrize("mode", ["crash", "type"])
def test_crashing_or_malformed_extractor_falls_back_to_reference(mode):
    out = pipeline(FakeOCR(ocr_result([ROW_LINES])), Broken(mode), fallback=RuleBasedExtractor()).run([img()])
    assert out.extractor.name == "rule-based-baseline" and out.fallback_from == "broken@0"
    assert out.extraction.data.total == Decimal("4746.00")


def test_no_fallback_configured_fails_safely_without_leaking_details():
    with pytest.raises(ExtractionError) as e:
        pipeline(FakeOCR(ocr_result([INVOICE])), Broken("crash")).run([img()])
    assert "secret" not in str(e.value) and "/srv" not in str(e.value)


@pytest.mark.parametrize("mode", ["currency", "garbage_amount", "huge"])
def test_invalid_values_from_an_extractor_never_become_data(mode):
    out = pipeline(FakeOCR(ocr_result([INVOICE])), Broken(mode)).run([img()])
    d = out.extraction.data
    assert d.total is None or d.total < Decimal("1e12")
    assert d.currency in (None, "USD", "NPR", "EUR", "INR", "GBP") and out.extraction.requires_review
    assert d.currency != "$$$$"


def test_confidence_is_reported_as_uncalibrated_heuristic():
    ex = pipeline(FakeOCR(ocr_result([INVOICE])), LayoutRulesExtractor()).run([img()]).extraction
    assert ex.calibrated is False and ex.confidence_method == "heuristic-v2" and 0 <= ex.confidence <= 1
    assert set(ex.field_confidence) >= {"vendor", "invoice_number", "date", "total"}
