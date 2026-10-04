"""Heuristic baseline extractor (regex/keyword rules over OCR lines).

This is NOT a trained document-understanding model. It exists so the pipeline is runnable
end-to-end and to provide a measurable baseline to beat. Replace with a layout-aware model
behind the same `Extractor` interface.
"""
from __future__ import annotations

import re

from app.extraction.base import Extractor, RawExtraction, RawField, RawItem
from app.extraction.normalize import detect_currency
from app.ocr.base import OCRLine, OCRResult

_NUM = r"-?[\d][\d,]*(?:\.\d{1,2})?"
# The keyword must be a whole word ("TAX INVOICE" must not match "INV" and capture "OICE"), and the number must contain a digit.
_INV_RE = re.compile(r"\b(?:invoice|inv|bill|receipt)\b\s*(?:no|number|num|#)?\.?\s*[:#\-]?\s*(?=[A-Z0-9\-/]*\d)([A-Z0-9][A-Z0-9\-/]{2,})", re.I)
_DATE_RE = re.compile(r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})")
_ITEM_RE = re.compile(rf"^(?P<d>.*?[A-Za-z].*?)\s+(?P<q>\d+(?:\.\d+)?)\s+(?P<p>{_NUM})\s+(?P<a>{_NUM})\s*$")
_SKIP_VENDOR = re.compile(r"invoice|receipt|bill\s*to|tax|date|page|tel|phone|pan|vat|www\.|@", re.I)


def _last_number(text: str) -> str | None:
    nums = re.findall(_NUM, text)
    return nums[-1] if nums else None


class RuleBasedExtractor(Extractor):
    name, version = "rule-based-baseline", "0.1.0"

    def extract(self, ocr: OCRResult, pages=None) -> RawExtraction:
        lines = ocr.lines
        page_of = {id(ln): pg.number for pg in ocr.pages for ln in pg.lines}
        out = RawExtraction()
        full = "\n".join(l.text for l in lines)

        cur = detect_currency(full)
        out.fields["currency"] = RawField(cur, 0.7 if cur else 0.0)

        out.fields["vendor"] = self._vendor(lines)
        out.fields["invoice_number"] = self._first(lines, _INV_RE, 0.95)
        dm = self._first(lines, _DATE_RE, 0.9)
        out.fields["date"] = dm
        for fld, method in (("vendor", "regex:first_text_line"), ("invoice_number", "regex:invoice_no"), ("date", "regex:date")):
            self._locate(out.fields[fld], lines, page_of, method)

        def amount(label: re.Pattern[str], exclude: re.Pattern[str] | None = None) -> RawField:
            for ln in reversed(lines):
                if label.search(ln.text) and not (exclude and exclude.search(ln.text)):
                    n = _last_number(ln.text)
                    if n:
                        return RawField(n, min(1.0, ln.confidence * 0.95), page_of.get(id(ln)), tuple(ln.bbox), ln.text,
                                        "regex:amount_label", ln.confidence)
            return RawField(None, 0.0)

        out.fields["subtotal"] = amount(re.compile(r"sub\s*-?\s*total", re.I))
        out.fields["tax"] = amount(re.compile(r"\b(vat|tax|gst)\b", re.I), re.compile(r"tax\s*invoice|taxable", re.I))
        total = amount(re.compile(r"(grand\s*total|total\s*amount|amount\s*due|net\s*amount|\btotal\b)", re.I),
                       re.compile(r"sub\s*-?\s*total", re.I))
        out.fields["total"] = total

        for ln in lines:
            m = _ITEM_RE.match(ln.text.strip())
            if m and not re.search(r"total|tax|vat", m["d"], re.I):
                out.items.append(RawItem(m["d"].strip(), m["q"], m["p"], m["a"], ln.confidence * 0.8,
                                         page=page_of.get(id(ln)), bbox=tuple(ln.bbox), source_text=ln.text))
        return out

    @staticmethod
    def _locate(f: RawField, lines: list[OCRLine], page_of: dict[int, int], method: str) -> None:
        """Attach source evidence (page, box, line text) to a field found by _first/_vendor. Never changes the value."""
        if f.value is None:
            return
        for ln in lines:
            if f.value in ln.text:
                f.page, f.bbox, f.source_text, f.method, f.ocr_confidence = page_of.get(id(ln)), tuple(ln.bbox), ln.text, method, ln.confidence
                return

    @staticmethod
    def _first(lines: list[OCRLine], rx: re.Pattern[str], strength: float) -> RawField:
        for ln in lines:
            m = rx.search(ln.text)
            if m:
                return RawField(m.group(1), min(1.0, ln.confidence * strength))
        return RawField(None, 0.0)

    @staticmethod
    def _vendor(lines: list[OCRLine]) -> RawField:
        for ln in lines[:6]:
            t = ln.text.strip()
            letters = sum(c.isalpha() for c in t)
            if len(t) >= 3 and letters / max(len(t), 1) > 0.6 and not _SKIP_VENDOR.search(t):
                return RawField(t, min(1.0, ln.confidence * 0.75))
        return RawField(None, 0.0)
