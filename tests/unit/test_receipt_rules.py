"""Till-receipt handling found on a real phone photo (tests/fixtures/receipt_winetown.png, an Indian GST cash memo).
The OCR lines below are what Tesseract returned for that photo after upscale+adaptive preprocessing (psm 6), noise included,
so the extractor is tested on realistic text without needing the engine."""
from datetime import date
from decimal import Decimal

from app.extraction.layout_rules import LayoutRulesExtractor
from app.extraction.normalize import detect_currency
from app.extraction.pipeline import DocumentPipeline
from app.extraction.schema import InvoiceData
from app.extraction.validation import validate
from app.ocr.base import OCRLine, OCRPage, OCRProvider, OCRResult, OCRWord
from PIL import Image

RECEIPT = [   # (text, height) - the shop name is printed larger than the other lines
    ("DUPLICATE ai", 30), ("WINE TOWN -", 44), ("Restaurant & Bar", 30), ("Hotel Viva In,Nr Cafe Coffee Day", 30),
    ("Chandani Chowk ,Bavdhan,Pune 411021 :", 30), ("weceeeee-—-> Cash Memo -~--~--------- '", 30),
    ("pate ; 09/04/22 Bill No. : 25", 30), ("T.No.: 10 W. No. 3:5", 30), ("part iculars Qty Rate Amount 7", 30),
    ("PANEER TIKKA 1 229 229 :", 30), ("FINGER CHIPS 1 139 139", 30), ("Sub Total : 368.00 =", 30),
    ("CGST €2.5% On 368 : 9.20 —", 30), ("SGST @2.5% On 368: ° 9.20 —~", 30), ("Food Total : 386.40 .", 30),
    ("BIRA BLONDE PITCHER 2 499 998", 30), ("_ Liquor Total : 998.00 :", 30), ("3/4 Total ; 1384 )", 30),
    ("GSTIN:27AAXFBS48J1Z) (05:34 PM)", 30), ("A DIVISION OF SHREE SAFARI HOTELS PVT LTD .", 30),
    ("-BHAREKAR & BROTHERS MULTI MGMT SERVICES LLP", 30), ("| CONT NO 9970439993 / 8459766126", 30)]


def ocr_from(rows, qty_x=620, rate_x=720, amt_x=860):
    """Lay words out left to right; the last three numeric words of an item row sit in the Qty/Rate/Amount columns."""
    lines, y = [], 10
    for text, h in rows:
        words, x = [], 20
        toks = text.split(" ")
        numeric_tail = [i for i, t in enumerate(toks) if t.replace(",", "").replace(".", "").isdigit()][-3:] if text.upper().split()[0] in (
            "PANEER", "FINGER", "BIRA", "PART", "RICE") else []
        for i, t in enumerate(toks):
            if i in numeric_tail and len(numeric_tail) == 3:
                x = (qty_x, rate_x, amt_x)[numeric_tail.index(i)]
            words.append(OCRWord(t, 0.9, (x, y, x + 12 * len(t), y + h))); x += 12 * (len(t) + 1)
        if text.startswith("part iculars"):
            words = [OCRWord("part", 0.9, (20, y, 68, y + h)), OCRWord("iculars", 0.9, (80, y, 164, y + h)),
                     OCRWord("Qty", 0.9, (qty_x, y, qty_x + 36, y + h)), OCRWord("Rate", 0.9, (rate_x, y, rate_x + 48, y + h)),
                     OCRWord("Amount", 0.9, (amt_x - 20, y, amt_x + 52, y + h)), OCRWord("7", 0.5, (980, y, 992, y + h))]
        lines.append(OCRLine(text, 0.9, (words[0].bbox[0], y, words[-1].bbox[2], y + h), words))
        y += h + 14
    return OCRResult("t", "1", [OCRPage(1, 1100, y + 50, lines)])


def extract(rows=RECEIPT):
    pipe = DocumentPipeline(_Replay(ocr_from(rows)), LayoutRulesExtractor(), day_first=True, review_threshold=0.8)
    return pipe.run([Image.new("RGB", (1100, 1600), "white")]).extraction


class _Replay(OCRProvider):
    name, version = "replay", "1"

    def __init__(self, result):
        self.result = result

    def recognize(self, pages):
        return self.result


def test_receipt_header_fields():
    d = extract().data
    assert d.vendor == "WINE TOWN"                                   # not the address line, not "DUPLICATE"
    assert "Chandani Chowk" in d.supplier_address and "Cash Memo" not in d.supplier_address
    assert d.invoice_number == "25"                                  # "Bill No. : 25": a short number behind an explicit "No."
    assert d.date == date(2022, 4, 9)
    assert d.document_type == "receipt"


def test_receipt_currency_inferred_from_gst_markers_not_from_ocr_noise():
    d = extract().data
    assert d.currency == "INR"                                       # CGST/SGST/GSTIN, and "€2.5%" (OCR of "@2.5%") is not euros
    assert detect_currency("CGST €2.5% On 368") is None
    assert detect_currency("Total € 25.00") == "EUR"                 # a real euro amount still counts


def test_receipt_totals_tax_components_and_grand_total():
    d = extract().data
    assert d.subtotal == Decimal("368.00")
    assert d.tax == Decimal("18.40")                                 # CGST 9.20 + SGST 9.20
    assert d.total == Decimal("1384")                                # the bare "Total", not "Food Total" or "Liquor Total"


def test_receipt_items_include_the_second_section_and_stop_at_noise():
    items = extract().data.items
    assert [(i.description, i.quantity, i.amount) for i in items] == [
        ("PANEER TIKKA", Decimal("1"), Decimal("229")), ("FINGER CHIPS", Decimal("1"), Decimal("139")),
        ("BIRA BLONDE PITCHER", Decimal("2"), Decimal("998"))]       # long name stays in the description column


def test_receipt_validates_without_error_issues():
    out = extract()
    assert [i.code for i in out.issues if i.severity == "error"] == []
    assert "items_sum_mismatch" not in [i.code for i in out.issues]  # printed Sub Total covers the food section only


def test_only_section_totals_leave_total_empty_instead_of_guessing():
    rows = [r for r in RECEIPT if not r[0].startswith("3/4 Total")]
    d = extract(rows).data
    assert d.total is None                                           # "Food Total" + "Liquor Total": the sum is unknown, review is forced


def test_single_section_total_is_used():
    rows = [r for r in RECEIPT if not r[0].startswith(("3/4 Total", "BIRA", "_ Liquor"))]
    assert extract(rows).data.total == Decimal("386.40")


def test_explicit_currency_beats_gst_inference():
    rows = RECEIPT + [("Currency: USD", 30)]
    assert extract(rows).data.currency == "USD"


def _inv(**kw):
    base = dict(document_type="receipt", currency="INR")
    base.update(kw)
    return InvoiceData(**base)


def test_whole_unit_round_off_is_accepted_but_real_differences_are_not():
    ok = _inv(subtotal=Decimal("368.00"), tax=Decimal("18.40"), total=Decimal("386"))     # 386.40 printed as 386
    assert "totals_mismatch" not in [i.code for i in validate(ok)]
    bad = _inv(subtotal=Decimal("368.00"), tax=Decimal("18.40"), total=Decimal("387"))
    assert "totals_mismatch" in [i.code for i in validate(bad)]
    fractional = _inv(subtotal=Decimal("368.00"), tax=Decimal("18.40"), total=Decimal("386.10"))   # not whole: no round-off allowance
    assert "totals_mismatch" in [i.code for i in validate(fractional)]
