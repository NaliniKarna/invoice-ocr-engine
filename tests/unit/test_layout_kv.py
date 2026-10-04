"""Geometry-based extraction on hand-built word boxes: no OCR engine, no browser. Each test is a different LAYOUT idea, not a vendor."""
from decimal import Decimal

from app.extraction.layout_kv import LayoutKVExtractor, find_date, id_like, money_value
from app.extraction.postprocess import build_output
from app.ocr.base import OCRLine, OCRPage, OCRResult, OCRWord

CH, H = 9, 20          # character width, text height in pixels


def doc(rows, width=900):
    """rows: list of (y, [(x, text), ...]). Words inside a text are 7 px apart; separate (x, text) items are separate columns."""
    lines = []
    for y, cells in rows:
        words = []
        for x, text in cells:
            cx = x
            for w in text.split(" "):
                words.append(OCRWord(w, 0.95, (cx, y, cx + CH * len(w), y + H)))
                cx += CH * len(w) + 7
        words.sort(key=lambda w: w.bbox[0])
        lines.append(OCRLine(" ".join(w.text for w in words), 0.95, (words[0].bbox[0], y, words[-1].bbox[2], y + H), words))
    return OCRResult("test", "1", [OCRPage(1, width, max(y for y, _ in rows) + 60, lines)])


def run(rows, **kw):
    raw = LayoutKVExtractor().extract(doc(rows, **kw))
    return build_output(raw, day_first=True, review_threshold=0.8)


def test_value_typing():
    assert money_value("$1,234.56") == Decimal("1234.56") and money_value("1.234,56 €") == Decimal("1234.56")
    assert money_value("& 9,414.22") == Decimal("9414.22")           # a currency sign OCR turned into "&"
    assert money_value("18%") is None and money_value("INV-2025") is None and money_value("Total") is None
    assert find_date("Invoice Date 20/10/2025")[1] == "20/10/2025" and find_date("24-Dec-2025")[1] == "24-Dec-2025"
    assert id_like("BL-66750") and not id_like("1,234.56") and not id_like("20/10/2025") and not id_like("Total")


LAYOUT_A = [   # leading Date column, "Taxes", currency symbols on every amount
    (20, [(40, "Orchid Lane Florists"), (600, "Invoice # BL-66750")]),
    (50, [(600, "Invoice Date 20/10/2025")]),
    (120, [(40, "Date"), (160, "Description"), (480, "Qty"), (560, "Rate"), (700, "Amount")]),
    (150, [(40, "20/10/2025"), (160, "LED panel light"), (480, "2"), (560, "$436.69"), (700, "$873.38")]),
    (180, [(40, "20/10/2025"), (160, "Delivery charge"), (480, "8"), (560, "$218.31"), (700, "$1,746.48")]),
    (240, [(560, "Sub Total"), (700, "$2,619.86")]),
    (270, [(560, "Taxes"), (700, "$209.59")]),
    (300, [(560, "Total"), (700, "$2,829.45")]),
]


def test_variable_labels_symbols_and_extra_column_without_vendor_rules():
    o = run(LAYOUT_A)
    d = o.data
    assert (d.vendor, d.invoice_number, str(d.date)) == ("Orchid Lane Florists", "BL-66750", "2025-10-20")
    assert (d.subtotal, d.tax, d.total) == (Decimal("2619.86"), Decimal("209.59"), Decimal("2829.45"))
    assert [(i.description, i.quantity, i.unit_price, i.amount) for i in d.items] == [
        ("LED panel light", Decimal(2), Decimal("436.69"), Decimal("873.38")), ("Delivery charge", Decimal(8), Decimal("218.31"), Decimal("1746.48"))]
    assert not [i for i in o.issues if i.severity == "error"]


def test_same_content_with_other_words_and_columns():
    """Same document, every label and column name swapped, one more unknown column: the pairs and the arithmetic still work."""
    rows = [
        (20, [(40, "Orchid Lane Florists")]), (50, [(40, "INVOICE NUMBER"), (300, "ISSUED")]), (74, [(40, "BL-66750"), (300, "20 October 2025")]),
        (120, [(40, "Article"), (300, "Hrs"), (400, "Colour"), (520, "Unit cost"), (700, "Sum")]),
        (150, [(40, "LED panel light"), (300, "2"), (400, "blue"), (520, "436.69"), (700, "873.38")]),
        (180, [(40, "Delivery charge"), (300, "8"), (400, "red"), (520, "218.31"), (700, "1,746.48")]),
        (240, [(520, "Total excl. VAT"), (700, "2,619.86")]), (270, [(520, "VAT (8%)"), (700, "209.59")]), (300, [(520, "Amount payable"), (700, "2,829.45")]),
    ]
    d = run(rows).data
    assert (d.vendor, d.invoice_number, str(d.date)) == ("Orchid Lane Florists", "BL-66750", "2025-10-20")
    assert (d.subtotal, d.tax, d.total) == (Decimal("2619.86"), Decimal("209.59"), Decimal("2829.45")) and len(d.items) == 2


def test_component_taxes_are_summed():
    rows = LAYOUT_A[:5] + [(240, [(560, "Sub Total"), (700, "2,619.86")]), (270, [(560, "CGST 9%"), (700, "235.79")]),
                           (300, [(560, "SGST 9%"), (700, "235.79")]), (330, [(560, "Grand Total"), (700, "3,091.44")])]
    d = run(rows).data
    assert d.tax == Decimal("471.58") and d.total == Decimal("3091.44")


def test_ambiguous_net_total_is_settled_by_arithmetic():
    rows = LAYOUT_A[:5] + [(240, [(560, "Net total"), (700, "2,619.86")]), (270, [(560, "Tax"), (700, "209.59")]), (300, [(560, "Amount payable"), (700, "2,829.45")])]
    d = run(rows).data
    assert (d.subtotal, d.tax, d.total) == (Decimal("2619.86"), Decimal("209.59"), Decimal("2829.45"))


def test_tax_inclusive_document_does_not_invent_a_subtotal():
    rows = LAYOUT_A[:5] + [(240, [(260, "Invoice total (prices include tax)"), (700, "2,619.86")]), (270, [(440, "of which VAT 20%"), (700, "436.64")])]
    d = run(rows).data
    assert d.total == Decimal("2619.86") and d.tax == Decimal("436.64") and d.subtotal is None


def test_several_section_totals_without_a_grand_total_stay_empty():
    rows = LAYOUT_A[:5] + [(240, [(560, "Food Total"), (700, "100.00")]), (270, [(560, "Liquor Total"), (700, "250.00")])]
    assert run(rows).data.total is None


def test_street_word_in_a_business_name_is_not_an_address_and_wrapped_names_join():
    rows = [(20, [(40, "Orchid Lane")]), (44, [(40, "Florists")]), (80, [(40, "12 Mill Road, Leeds")])] + LAYOUT_A[2:]
    assert run(rows).data.vendor == "Orchid Lane Florists"


def test_bill_to_block_is_not_the_vendor():
    rows = [(20, [(40, "Bill To")]), (44, [(40, "Maple Dental Clinic")]), (68, [(40, "44 Elm Street")]),
            (110, [(500, "Brightwater Studio LLC")])] + [(y + 40, c) for y, c in LAYOUT_A[2:]]
    assert run(rows).data.vendor == "Brightwater Studio LLC"


def test_receipt_without_header_gets_items():
    rows = [(20, [(300, "CORNER STORE")]), (50, [(40, "Receipt No: 0042"), (500, "18-Sep-2025")]),
            (90, [(40, "Cable tie pack"), (400, "3 x 12.00"), (600, "36.00")]), (120, [(40, "Markers"), (400, "2 x 4.50"), (600, "9.00")]),
            (170, [(40, "SUBTOTAL"), (600, "45.00")]), (200, [(40, "GST 5%"), (600, "2.25")]), (230, [(40, "TOTAL"), (600, "47.25")])]
    d = run(rows).data
    assert [i.amount for i in d.items] == [Decimal("36.00"), Decimal("9.00")] and str(d.date) == "2025-09-18" and d.invoice_number == "0042"
    assert (d.subtotal, d.tax, d.total) == (Decimal("45.00"), Decimal("2.25"), Decimal("47.25"))


def test_unknown_language_yields_missing_fields_not_wrong_ones():
    rows = [(20, [(40, "Quartz Software GmbH")]), (120, [(40, "Bezeichnung"), (480, "Menge"), (700, "Gesamt")]), (150, [(40, "Lizenz"), (480, "2"), (700, "10,00")]),
            (240, [(560, "Nettobetrag"), (700, "10,00")]), (270, [(560, "Gesamtbetrag"), (700, "11,90")])]
    d = run(rows).data
    assert d.total is None and d.vendor == "Quartz Software GmbH"          # vocabulary gap: reported as missing, never guessed
