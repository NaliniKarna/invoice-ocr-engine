"""Real Tesseract on a real phone photo of a till receipt (423x694 px, blue-tinted, creased). Skipped without the binary.
Before this change the same photo returned vendor "Chandani Chowk ,Bavdhan Pune nan", no total, no items, confidence 0.083."""
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "receipt_winetown.png"


def _pipeline(with_retry: bool):
    from app.extraction.layout_rules import LayoutRulesExtractor
    from app.extraction.pipeline import DocumentPipeline, RetryPlan
    from app.ocr.providers import TesseractProvider
    retry = RetryPlan(TesseractProvider(psm=6), ["upscale", "adaptive"], psm=6) if with_retry else None
    return DocumentPipeline(TesseractProvider(), LayoutRulesExtractor(), day_first=True, review_threshold=0.8, retry=retry)


def _page():
    from app.ocr.preprocess import _normalise
    return [_normalise(Image.open(FIXTURE))]


def test_first_pass_alone_is_poor_so_the_retry_is_needed():
    out = _pipeline(False).run(_page()).extraction
    assert out.data.total is None and any(i.severity == "error" for i in out.issues)


def test_retry_recovers_the_receipt():
    run = _pipeline(True).run(_page())
    d = run.extraction.data
    assert run.retry["used"] is True
    assert d.vendor == "WINE TOWN" and d.invoice_number == "25" and d.date == date(2022, 4, 9)
    assert d.currency == "INR" and d.subtotal == Decimal("368.00") and d.tax == Decimal("18.40") and d.total == Decimal("1384")
    assert [(i.description, i.amount) for i in d.items] == [("PANEER TIKKA", Decimal("229")), ("FINGER CHIPS", Decimal("139")),
                                                            ("BIRA BLONDE PITCHER", Decimal("998"))]
    assert not [i for i in run.extraction.issues if i.severity == "error"]
