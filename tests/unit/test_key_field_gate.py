"""Key-field gate: missing or suspicious vendor / invoice number / date / total drive another extractor and another OCR pass,
even when the overall confidence is above every threshold."""
from datetime import date
from decimal import Decimal

import pytest
from PIL import Image

from app.core.config import Settings
from app.extraction.base import Extractor, RawExtraction, RawField
from app.extraction.pipeline import DocumentPipeline, RetryPlan
from app.extraction.rules import RuleBasedExtractor
from app.extraction.schema import InvoiceData
from app.extraction.validation import GATE_CODES, ValidationConfig, validate
from app.ocr.providers import parse_retry_passes, retry_variants
from tests.fakes import FakeOCR, INVOICE_LINES

TODAY = date(2026, 10, 1)
REQ = ValidationConfig(required_fields=True)
D = Decimal


def codes(cfg=REQ, **kw):
    return {i.code for i in validate(InvoiceData(**kw), today=TODAY, cfg=cfg)}


# ------------------------------------------------------------------------------------------------ validation
def test_required_fields_are_reported_only_when_asked():
    assert codes(cfg=ValidationConfig(), total=D("10"), currency="USD") == set()               # a partial human-corrected truth is legitimate
    assert {"missing_vendor", "missing_invoice_number", "missing_date"} <= codes(total=D("10"))


def test_complete_document_has_no_gate_issue():
    assert not codes(vendor="Acme Ltd", invoice_number="INV-1", date=date(2026, 9, 1), subtotal=D("100"), tax=D("10"), total=D("110")) & GATE_CODES


@pytest.mark.parametrize("vendor", ["Total", "Invoice Date", "20/10/2025", "$1,234.56", "12", "Bill To"])
def test_suspicious_vendor(vendor):
    assert "suspicious_vendor" in codes(vendor=vendor, invoice_number="INV-1", date=date(2026, 9, 1), total=D("10"))


@pytest.mark.parametrize("number", ["20/10/2025", "1,234.56", "A", "Invoice"])
def test_suspicious_invoice_number(number):
    assert "suspicious_invoice_number" in codes(vendor="Acme Ltd", invoice_number=number, date=date(2026, 9, 1), total=D("10"))


def test_tax_and_subtotal_are_flagged_only_when_the_numbers_imply_them():
    base = dict(vendor="Acme Ltd", invoice_number="INV-1", date=date(2026, 9, 1))
    assert "missing_tax" in codes(**base, subtotal=D("100"), total=D("118"))          # total is higher than the subtotal: some tax exists
    assert "missing_tax" not in codes(**base, total=D("118"))                         # a lone total (till receipt) is normal
    assert "missing_tax" not in codes(**base, subtotal=D("100"), total=D("100"))
    assert "missing_subtotal" in codes(**base, tax=D("18"), total=D("118"), taxable_amount=D("100"))
    assert "missing_subtotal" not in codes(**base, tax=D("18"), total=D("118"))


def test_items_without_any_amount_are_flagged():
    from app.extraction.schema import LineItem
    assert "items_without_amounts" in codes(vendor="Acme Ltd", invoice_number="INV-1", date=date(2026, 9, 1), total=D("1"), items=[LineItem(description="x")])


# ------------------------------------------------------------------------------------------------ alternates
class Stub(Extractor):
    def __init__(self, name, **fields):
        self.name, self.version = name, "1"
        self.fields, self.calls = fields, 0

    def extract(self, ocr, pages=None):
        self.calls += 1
        return RawExtraction(fields={k: RawField(v, 0.95) for k, v in self.fields.items()})


FULL = dict(vendor="Acme Traders Ltd", invoice_number="INV-7", date="15/03/2026", total="118.00", subtotal="100.00", tax="18.00", currency="USD")
NO_NUMBER = {k: v for k, v in FULL.items() if k != "invoice_number"}


def pipe(primary, alternates=(), **kw):
    return DocumentPipeline(FakeOCR(), primary, day_first=True, review_threshold=0.8, validation=REQ, alternates=alternates, **kw)


def img():
    return [Image.new("RGB", (300, 400), "white")]


def test_alternate_with_fewer_key_field_problems_wins_even_at_high_confidence():
    a, b = Stub("primary", **NO_NUMBER), Stub("alt", **FULL)
    out = pipe(a, [b]).run(img())
    assert out.extraction.confidence > 0.5                     # overall confidence alone would not have triggered anything
    assert out.extractor is b and out.extraction.data.invoice_number == "INV-7"
    assert [s["chosen"] for s in out.strategies] == [False, True] and out.strategies[0]["gate"] == ["missing_invoice_number"]


def test_alternates_are_not_run_when_the_primary_is_clean():
    a, b = Stub("primary", **FULL), Stub("alt", **NO_NUMBER)
    out = pipe(a, [b]).run(img())
    assert b.calls == 0 and out.extractor is a and out.strategies is None


def test_primary_wins_ties_and_a_crashing_alternate_is_skipped():
    class Boom(Stub):
        def extract(self, ocr, pages=None):
            raise RuntimeError("x")
    a, c = Stub("primary", **NO_NUMBER), Boom("boom")
    d = Stub("same", **NO_NUMBER)
    out = pipe(a, [c, d]).run(img())
    assert out.extractor is a and any("error" in s for s in out.strategies)


def test_alternate_never_replaces_a_result_with_more_errors():
    wrong = dict(FULL, total="500.00")                                         # totals_mismatch: error
    a, b = Stub("primary", **NO_NUMBER), Stub("alt", **wrong)
    assert pipe(a, [b]).run(img()).extractor is a


# ------------------------------------------------------------------------------------------------ OCR retry on key fields
LINES_NO_NUMBER = [ln for ln in INVOICE_LINES if "Invoice No" not in ln]


class Counting(FakeOCR):
    name = "counting"


def retry_pipe(first, plans, **kw):
    return DocumentPipeline(first, RuleBasedExtractor(), day_first=True, review_threshold=0.8, validation=REQ, retry=plans, **kw)


def test_retry_fires_on_a_missing_key_field_although_confidence_is_above_the_threshold():
    first, second = FakeOCR(LINES_NO_NUMBER), Counting(INVOICE_LINES)
    out = retry_pipe(first, RetryPlan(second, ["upscale"])).run(img())
    assert out.retry["trigger"] == "key_field" and second.calls == 1 and out.retry["used"] is True
    assert out.extraction.data.invoice_number == "INV-2026-0042"
    assert "missing_invoice_number" in out.retry["first"]["gate"]


def test_key_field_retry_can_be_turned_off():
    first, second = FakeOCR(LINES_NO_NUMBER), Counting(INVOICE_LINES)
    out = retry_pipe(first, RetryPlan(second, ["upscale"], on_key_fields=False)).run(img())
    assert second.calls == 0 and out.retry is None


def test_variants_run_in_order_and_stop_when_the_result_is_clean():
    first, v1, v2 = FakeOCR(LINES_NO_NUMBER), Counting(INVOICE_LINES), Counting(INVOICE_LINES)
    out = retry_pipe(first, [RetryPlan(v1, ["upscale"], psm=3), RetryPlan(v2, ["upscale", "adaptive"], psm=6)]).run(img())
    assert v1.calls == 1 and v2.calls == 0 and out.retry["ops"] == ["upscale"] and len(out.retry["passes"]) == 1


def test_second_variant_runs_when_the_first_does_not_help():
    first, v1, v2 = FakeOCR(LINES_NO_NUMBER), Counting(LINES_NO_NUMBER), Counting(INVOICE_LINES)
    out = retry_pipe(first, [RetryPlan(v1, ["upscale"], psm=3), RetryPlan(v2, ["upscale", "adaptive"], psm=6)]).run(img())
    assert v1.calls == 1 and v2.calls == 1 and out.retry["used"] is True and out.retry["ops"] == ["upscale", "adaptive"] and out.retry["psm"] == 6
    assert [p["used"] for p in out.retry["passes"]] == [False, True]


# ------------------------------------------------------------------------------------------------ settings
def test_retry_passes_parse_and_validate():
    assert parse_retry_passes("upscale@3;upscale,adaptive@6") == [(["upscale"], 3), (["upscale", "adaptive"], 6)]
    assert parse_retry_passes("upscale", 6) == [(["upscale"], 6)] and parse_retry_passes("") == []
    for bad in ("upscale@7", "bogus@3", "@3"):
        with pytest.raises(ValueError):
            parse_retry_passes(bad)
    with pytest.raises(ValueError):
        Settings(retry_passes="upscale@9")


def test_retry_variants_fall_back_to_the_single_pair():
    assert retry_variants(Settings(retry_preprocess_ops="upscale,adaptive", retry_tesseract_psm=6)) == [(["upscale", "adaptive"], 6)]
    assert retry_variants(Settings(retry_passes="upscale@3", retry_preprocess_ops="adaptive")) == [(["upscale"], 3)]
    assert retry_variants(Settings()) == []


def test_extractor_settings_are_validated():
    assert Settings(reference_extractor="layout-kv", extractor_alternates="layout-rules, rule-based-baseline").reference_extractor == "layout-kv"
    with pytest.raises(ValueError):
        Settings(extractor_alternates="layout-kv,made-up")
