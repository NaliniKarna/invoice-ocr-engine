"""Layout-aware deterministic extractor ("layout-rules").

Uses row/column structure from the layout analyzer instead of free-text regex over OCR lines:
* key-value pairs from rows ("Invoice No: X"), restricted to the header region;
* supplier block (name, address, phone, email, tax id) and customer block ("Bill To");
* totals from the totals region, label -> amount on the same row;
* line items from the detected table by column position (works across pages), with a per-row regex fallback when no
  table header exists (receipts).
Not a trained model: it encodes common invoice conventions. Its accuracy on real invoices is unmeasured.
"""
from __future__ import annotations

import re

from app.extraction.base import Extractor, RawExtraction, RawField, RawItem
from app.extraction.normalize import detect_currency
from app.layout.base import Layout, LayoutAnalyzer, Row
from app.layout.heuristic import NUM, HeuristicLayoutAnalyzer, table_items
from app.ocr.base import OCRResult

_D = r"[\d०-९Ool|I]"
_DATE_TOKEN = (rf"({_D}{{1,4}}[-/.]{_D}{{1,2}}[-/.]{_D}{{1,4}}|\d{{1,2}}\s+[A-Za-z]{{3,9}}\.?,?\s+\d{{4}}|[A-Za-z]{{3,9}}\.?\s+\d{{1,2}},?\s+\d{{4}})")
# "Invoice No: X", "Invoice #X" and plain "Invoice: X". Whole-word keyword; the number must contain a digit.
_INV = re.compile(r"\b(?:i?nvoice|inv|bill|receipt|voucher)\b\s*(?:(?:no|number|num|#)\.?\s*[:#\-]?|[:#\-])\s*(?=[A-Z0-9\-/]*\d)([A-Z0-9][A-Z0-9\-/]{2,})", re.I)
# Explicit "No."/"Number"/"#" label: short numbers are normal on till receipts ("Bill No. : 25"). A label without
# "No" still needs >= 3 characters (above), so "Invoice: 1" is not taken as a number.
_INV_SHORT = re.compile(r"\b(?:i?nvoice|inv|bill|receipt|voucher)\s*(?:no|number|num|#)\.?\s*[:#\-]?\s*(?=[A-Z0-9\-/]*\d)([A-Z0-9][A-Z0-9\-/]*)", re.I)
_DOC_NO = re.compile(r"(?:document|doc|ref(?:erence)?)\s*(?:no|number|#)\.?\s*[:#\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I)
_DUE = re.compile(rf"(?:due\s*date|payment\s*due|due\s*on|due)\s*[:\-]?\s*{_DATE_TOKEN}", re.I)
_DATE = re.compile(rf"(?<!due )(?<!due)(invoice\s*date|bill\s*date|date|miti|मिति)\s*[:\-]?\s*{_DATE_TOKEN}", re.I)
_TAXID = re.compile(r"(?:PAN|VAT|GSTIN|TIN|TAX\s*ID)\s*(?:No\.?|Number|Reg\.?|ID)?\s*[:\-]?\s*([0-9][0-9\- ]{6,14}[0-9])", re.I)
_PHONE = re.compile(r"(?:tel|phone|ph|mob(?:ile)?|contact)\.?\s*[:\-]?\s*(\+?\d[\d\s\-]{5,16}\d)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BILL_TO = re.compile(r"^\s*(bill(?:ed)?\s*to|customer|sold\s*to|buyer|client)\b\s*:?\s*(.*)$", re.I)
_TITLE = re.compile(r"^\s*(tax\s+)?(invoice|receipt|bill|cash\s*memo|credit\s*note)\s*$", re.I)
_LABELLED = re.compile(r"\b(invoice|inv|bill|date|miti|due|tel|phone|pan|vat|email|gstin|page|receipt)\b", re.I)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Lines that are not the supplier name: copy markers, document titles, separators.
_NOT_VENDOR = re.compile(r"\b(duplicate|original|triplicate|copy|customer\s*copy|cash\s*memo|tax\s*invoice|invoice|receipt|bill\s*of\s*supply|credit\s*note|welcome|thank)\b", re.I)
# Address-looking line: street words or a postal-code-like digit run.
_ADDRESS_LIKE = re.compile(r"\d{3,}|\b(road|rd|street|st|nr|near|opp|opposite|floor|chowk|marg|nagar|lane|avenue|plaza|tower|sector|block|phase)\b", re.I)


def _is_junk_line(text: str) -> bool:
    """Separator lines and document titles ("-- Cash Memo --"): never part of a name or address."""
    letters = re.sub(r"[^A-Za-z ]", " ", text)
    return bool(_NOT_VENDOR.search(letters)) and len(letters.split()) <= 6

# label -> field, first match wins; `total` is last so "sub total"/"total discount"/"taxable" never hit it
_TOTAL_LABELS: list[tuple[str, re.Pattern[str]]] = [
    ("subtotal", re.compile(r"sub\s*-?\s*total", re.I)),
    ("discount", re.compile(r"discount|less\s*:", re.I)),
    ("taxable_amount", re.compile(r"taxable", re.I)),
    ("amount_due", re.compile(r"amount\s*due|balance|outstanding|due\s*amount", re.I)),
    ("amount_paid", re.compile(r"\bpaid\b|advance|received|payment", re.I)),
    ("service_charge", re.compile(r"service\s*(charge|fee)|svc", re.I)),
    ("other_charges", re.compile(r"shipping|delivery|freight|other\s*charges|packing|handling", re.I)),
    ("tax", re.compile(r"\b(?:[cs]|i|ut)?gst\b|\b(vat|tax)\b", re.I)),
    ("total", re.compile(r"grand\s*total|net\s*(amount|payable)|total\s*amount|amount\s*payable|\btotal\b", re.I)),
]
_GST_MARKERS = re.compile(r"\bGSTIN\b|\b[CSI]GST\b")
_TAX_COMPONENT = re.compile(r"\b(?:[cs]|i|ut)gst\b", re.I)    # CGST / SGST / IGST / UTGST: components that add up to the tax
_STRONG_TOTAL = re.compile(r"grand\s*total|net\s*(amount|payable)|amount\s*payable", re.I)


def _rf(value: str | None, row: Row | None, strength: float, method: str) -> RawField:
    if value is None or row is None:
        return RawField(None, 0.0)
    return RawField(value, min(1.0, row.confidence * strength), page=row.page, bbox=row.bbox, source_text=row.text,
                    method=method, ocr_confidence=round(row.confidence, 3))


class LayoutRulesExtractor(Extractor):
    name, version = "layout-rules", "0.1.0"

    def __init__(self, analyzer: LayoutAnalyzer | None = None):
        self.analyzer = analyzer or HeuristicLayoutAnalyzer()

    def extract(self, ocr: OCRResult, pages=None) -> RawExtraction:
        layout = self.analyzer.analyze(ocr)
        out = RawExtraction()
        rows = layout.rows
        full = "\n".join(r.text for r in rows)
        first = layout.pages[0] if layout.pages else None
        hdr_end = self._header_end(layout)
        head = [r for p in layout.pages[:1] for r in p.rows[:hdr_end]] if first else []

        cur = detect_currency(full)
        out.fields["currency"] = RawField(cur, 0.7 if cur else 0.0, method="text:currency_symbol")
        if cur is None and _GST_MARKERS.search(full):
            # Indian GST receipts rarely print a currency symbol. GSTIN / CGST / SGST / IGST exist only under the Indian GST
            # system, so the currency is INR. Lower confidence than a printed symbol.
            out.fields["currency"] = RawField("INR", 0.6, method="inferred:gst_marker")
        dev = len(_DEVANAGARI.findall(full))
        alpha = len(re.findall(r"[A-Za-z]", full)) + dev
        if alpha:
            out.fields["language"] = RawField("ne" if dev / alpha > 0.3 else "en", 0.6, method="script_ratio")
        out.fields["document_type"] = self._doc_type(full)

        self._header_fields(out, head, rows)
        self._totals(out, layout)
        out.items = self._items(layout)
        return out

    # ------------------------------------------------------------------ header region
    @staticmethod
    def _header_end(layout: Layout) -> int:
        if not layout.pages:
            return 0
        p = layout.pages[0]
        if p.tables:
            return max(0, p.rows.index(p.tables[0].header)) if p.tables[0].header in p.rows else len(p.rows)
        return min(len(p.rows), 14)

    def _header_fields(self, out: RawExtraction, head: list[Row], all_rows: list[Row]) -> None:
        search = head or all_rows[:14]
        inv = dt = due = docno = None
        for r in search:
            t = r.text
            if inv is None and (m := (_INV.search(t) or _INV_SHORT.search(t))):
                inv = _rf(m.group(1), r, 0.95, "layout:key_value")
            if docno is None and (m := _DOC_NO.search(t)):
                docno = _rf(m.group(1), r, 0.9, "layout:key_value")
            if due is None and (m := _DUE.search(t)):
                due = _rf(m.group(1), r, 0.9, "layout:key_value")
            elif dt is None and (m := _DATE.search(t)):
                label = m.group(1).lower()
                val = ("Miti " if label in ("miti", "मिति") else "") + m.group(2)
                dt = _rf(val, r, 0.92, "layout:key_value")
        if dt is None:          # fall back to the first date-looking token anywhere in the header
            for r in search:
                if (m := re.search(_DATE_TOKEN, r.text)):
                    dt = _rf(m.group(1), r, 0.7, "layout:first_date"); break
        out.fields["invoice_number"] = inv or RawField(None, 0.0)
        out.fields["document_number"] = docno or RawField(None, 0.0)
        out.fields["date"] = dt or RawField(None, 0.0)
        out.fields["due_date"] = due or RawField(None, 0.0)

        # customer block
        cust_rows: set[int] = set()
        for i, r in enumerate(search):
            m = _BILL_TO.match(r.text)
            if not m:
                continue
            name_row = None
            if m.group(2).strip():
                out.fields["customer_name"] = _rf(m.group(2).strip(), r, 0.85, "layout:customer_block"); name_row = i
            elif i + 1 < len(search):
                out.fields["customer_name"] = _rf(search[i + 1].text, search[i + 1], 0.85, "layout:customer_block"); name_row = i + 1
            cust_rows.add(i)
            if name_row is not None:
                cust_rows.add(name_row)
                addr = [x for x in search[name_row + 1:name_row + 3] if not _LABELLED.search(x.text) and not _TITLE.match(x.text)
                        and not _BILL_TO.match(x.text) and re.search(r"[A-Za-z]", x.text)]
                if addr:
                    out.fields["customer_address"] = _rf(", ".join(a.text for a in addr), addr[0], 0.8, "layout:customer_block")
                    cust_rows.update(search.index(a) for a in addr)
            break

        # supplier block: tallest text among the leading rows that are not titles/labels
        cand = [(i, r) for i, r in enumerate(search[:8]) if i not in cust_rows and not _TITLE.match(r.text)
                and not _LABELLED.search(r.text) and sum(c.isalpha() for c in r.text) / max(len(r.text), 1) > 0.6 and len(r.text) >= 3]
        # copy markers ("DUPLICATE"), titles and address lines are not the supplier name; fall back to them only if nothing else exists
        named = [(i, r) for i, r in cand if not _is_junk_line(r.text) and not _ADDRESS_LIKE.search(r.text)]
        vrow = max(named or cand, key=lambda ir: (ir[1].height, -ir[0]), default=None)
        if vrow:
            out.fields["vendor"] = _rf(vrow[1].text.strip(" -:|'\"._~=—"), vrow[1], 0.8, "layout:largest_header_text")   # edge noise from OCR (stray dashes, colons)
            nxt = [r for r in search[vrow[0] + 1:vrow[0] + 3]
                   if not _LABELLED.search(r.text) and not _TITLE.match(r.text) and not _BILL_TO.match(r.text)
                   and not _is_junk_line(r.text) and re.search(r"[A-Za-z]", r.text)]
            lookahead = [r for r in search[vrow[0] + 1:vrow[0] + 5]
                         if not _LABELLED.search(r.text) and not _BILL_TO.match(r.text) and not _is_junk_line(r.text)
                         and _ADDRESS_LIKE.search(r.text)]
            if lookahead:      # a tagline ("Restaurant & Bar") may sit between the name and the address: keep the address rows
                nxt = lookahead
            if nxt:
                out.fields["supplier_address"] = _rf(", ".join(r.text.strip(" -:|'\"._~=—") for r in nxt), nxt[0], 0.7, "layout:below_vendor")
        else:
            out.fields["vendor"] = RawField(None, 0.0)
        for i, r in enumerate(search):
            if "supplier_phone" not in out.fields and (m := _PHONE.search(r.text)) and i not in cust_rows:
                out.fields["supplier_phone"] = _rf(m.group(1).strip(), r, 0.9, "layout:key_value")
            if "supplier_email" not in out.fields and (m := _EMAIL.search(r.text)) and i not in cust_rows:
                out.fields["supplier_email"] = _rf(m.group(0), r, 0.95, "layout:pattern")
            if (m := _TAXID.search(r.text)):
                key = "customer_tax_id" if i in cust_rows else "supplier_tax_id"
                if key not in out.fields:
                    out.fields[key] = _rf(m.group(1).strip(), r, 0.9, "layout:key_value")

    @staticmethod
    def _doc_type(full: str) -> RawField:
        t = full.lower()
        for kw, typ in (("credit note", "credit_note"), ("receipt", "receipt"), ("cash memo", "receipt"), ("invoice", "invoice"), ("bill", "invoice")):
            if kw in t:
                return RawField(typ, 0.6, method="keyword")
        return RawField(None, 0.0)

    # ------------------------------------------------------------------ totals
    @staticmethod
    def _totals(out: RawExtraction, layout: Layout) -> None:
        # scan from the last page backwards: totals sit at the end of the document
        best_total_strength = 0
        section_totals: list[tuple[Row, str]] = []        # "Food Total", "Liquor Total": parts of a larger total
        tax_parts: list[tuple[Row, str]] = []             # CGST + SGST (or IGST): one tax made of several printed lines
        for p in reversed(layout.pages):
            if not p.rows:
                continue
            start = p.totals_start if p.totals_start is not None else max(0, len(p.rows) - 10)
            found_any = False
            for r in p.rows[start:]:
                nums = [c for c in r.cells if NUM.match(c.text.replace(" ", ""))]
                if not nums:
                    continue
                label = " ".join(c.text for c in r.cells if c.bbox[0] < nums[-1].bbox[0])
                for field_name, rx in _TOTAL_LABELS:
                    if rx.search(label):
                        if field_name == "tax" and re.search(r"taxable|tax\s*invoice", label, re.I):
                            continue
                        if field_name == "tax":
                            if _TAX_COMPONENT.search(label):      # "CGST @2.5% On 368 : 9.20": the amount is the last number
                                tax_parts.append((r, nums[-1].text)); found_any = True; break
                            if "tax" in out.fields and out.fields["tax"].value:
                                break
                            out.fields["tax"] = _rf(nums[-1].text, r, 0.93, "layout:totals_region"); found_any = True
                            break
                        if field_name == "total":
                            # 3 = grand/net/payable, 2 = a bare "Total", 1 = "<something> Total" (a section of the bill)
                            letters = re.sub(r"[^A-Za-z ]", " ", label).split()
                            strength = 3 if _STRONG_TOTAL.search(label) else 2 if letters and letters[-1].lower() == "total" and len(letters) == 1 else 1
                            if strength == 1:
                                section_totals.append((r, nums[-1].text)); found_any = True; break
                            if strength <= best_total_strength:
                                break
                            best_total_strength = strength
                            out.fields["total"] = _rf(nums[-1].text, r, 0.93, "layout:totals_region")
                            found_any = True
                            break
                        if field_name in out.fields and out.fields[field_name].value:
                            break
                        out.fields[field_name] = _rf(nums[-1].text, r, 0.93, "layout:totals_region")
                        found_any = True
                        break
            if found_any:
                break
        if tax_parts and not (out.fields.get("tax") and out.fields["tax"].value):
            from decimal import Decimal, InvalidOperation
            try:
                amount = sum((Decimal(v.replace(",", "")) for _, v in tax_parts), Decimal(0))
                out.fields["tax"] = _rf(f"{amount:.2f}", tax_parts[0][0], 0.9, "layout:tax_components")
            except InvalidOperation:
                pass
        if "total" not in out.fields or not out.fields["total"].value:
            # Only "<section> Total" lines exist. One of them is the total; several are parts of an unknown sum, so a
            # guess would be wrong silently: leave it empty and let validation send the document to review.
            if len(section_totals) == 1:
                row, val = section_totals[0]
                out.fields["total"] = _rf(val, row, 0.8, "layout:totals_region")
        for k in ("subtotal", "tax", "total"):
            out.fields.setdefault(k, RawField(None, 0.0))

    # ------------------------------------------------------------------ line items
    @staticmethod
    def _items(layout: Layout) -> list[RawItem]:
        items: list[RawItem] = []
        tables = layout.tables
        for t in tables:
            for it in table_items(t):
                if not (it.get("description") or "").strip():
                    continue
                items.append(RawItem(it["description"], it.get("quantity") or None, it.get("unit_price") or None,
                                     it.get("amount") or None, min(1.0, it["confidence"] * 0.9), unit=(re.sub(r"^[^\w]+|[^\w.]+$", "", it.get("unit") or "") or None),
                                     discount=it.get("discount") or None, tax=it.get("tax") or None, page=it["page"],
                                     bbox=it["bbox"], source_text=it["text"]))
        if items:
            return items
        # no table header (receipts): per-row "description qty price amount"
        rx = re.compile(rf"^(?P<d>.*?[A-Za-z].*?)\s+(?P<q>\d+(?:\.\d+)?)\s+(?P<p>{NUM.pattern[1:-1]})\s+(?P<a>{NUM.pattern[1:-1]})\s*$")
        for r in layout.rows:
            m = rx.match(r.text.strip())
            if m and not re.search(r"total|tax|vat|discount", m["d"], re.I):
                items.append(RawItem(m["d"].strip(), m["q"], m["p"], m["a"], r.confidence * 0.8, page=r.page, bbox=r.bbox, source_text=r.text))
        return items
