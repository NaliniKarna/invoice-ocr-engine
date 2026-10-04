"""Second OCR pass: when it runs, when it wins, and that it can never make a job worse."""
import pytest
from PIL import Image

from app.extraction.layout_rules import LayoutRulesExtractor
from app.extraction.pipeline import DocumentPipeline, RetryPlan
from app.extraction.rules import RuleBasedExtractor
from app.ocr.base import OCRLine, OCRPage, OCRPermanentError, OCRProvider, OCRResult
from tests.fakes import FakeOCR, INVOICE_LINES

GOOD = INVOICE_LINES
POOR = ["Himalayan Traders Pvt Ltd", "Invoice No: INV-2026-0042", "Date: 15/03/2026"]       # no totals: missing_total error


class Counting(FakeOCR):
    name = "counting"


def pipe(first, second, **plan):
    retry = RetryPlan(second, ["upscale"], **plan) if second is not None else None
    return DocumentPipeline(first, RuleBasedExtractor(), day_first=True, review_threshold=0.8, retry=retry)


def img():
    return [Image.new("RGB", (300, 400), "white")]


def test_good_first_result_never_triggers_a_retry():
    first, second = FakeOCR(GOOD), Counting(GOOD)
    out = pipe(first, second).run(img())
    assert second.calls == 0 and out.retry is None


def test_poor_first_result_is_replaced_by_a_better_second_pass():
    first, second = FakeOCR(POOR), Counting(GOOD)
    out = pipe(first, second).run(img())
    assert second.calls == 1 and out.retry["used"] is True
    assert out.extraction.data.total is not None and out.retry["first"]["errors"] >= 1 and out.retry["second"]["errors"] == 0
    assert out.ocr.provider == "counting"                       # the stored OCR result is the winning pass


def test_second_pass_that_is_not_better_is_discarded():
    first, second = FakeOCR(POOR), Counting(POOR)
    out = pipe(first, second).run(img())
    assert second.calls == 1 and out.retry["attempted"] and out.retry["used"] is False
    assert out.ocr.provider == "fake-ocr"


def test_second_pass_crash_keeps_the_first_result():
    first, second = FakeOCR(POOR), Counting(GOOD)
    second.fail_with = OCRPermanentError("boom")
    out = pipe(first, second).run(img())
    assert out.retry["used"] is False and "error" in out.retry and out.extraction is not None


def test_retry_is_skipped_for_unfixable_issues_and_long_documents():
    class PerPage(Counting):
        def recognize(self, pages):
            res = super().recognize(pages)
            return OCRResult(res.provider, res.model_version, [OCRPage(n, 100, 200, res.pages[0].lines) for n in range(1, len(pages) + 1)])

    deva = FakeOCR(["नमस्ते नेपाल बिल नम्बर १२३४५६ मिति २०८२"] * 3)
    second = Counting(GOOD)
    out = pipe(deva, second).run(img())
    assert second.calls == 0                                     # unsupported_script: another image treatment cannot read it
    second2 = PerPage(GOOD)
    pipe(PerPage(POOR), second2, max_pages=2).run(img() * 3)
    assert second2.calls == 0                                    # more pages than max_pages


def test_no_retry_plan_means_single_pass():
    out = pipe(FakeOCR(POOR), None).run(img())
    assert out.retry is None
