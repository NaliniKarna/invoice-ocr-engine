"""normalise -> validate -> score. Model-agnostic: works on any Extractor's RawExtraction.

Confidence produced here is HEURISTIC and uncalibrated (method "heuristic-v2"): OCR confidence of the source text times a
per-rule strength, reduced for risky normalization, ambiguity and validation problems. It is useful for ordering
and for deciding what a human should look at; it is not a probability of being correct. Calibration lives in
app/ml/calibration.py and is applied only when a calibrator trained on labelled data is active.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from app.extraction.base import RawExtraction, RawField
from app.extraction.normalization import (Normalized, normalize_date, normalize_money, normalize_quantity,
                                          normalize_text)
from app.extraction.normalize import normalize_currency
from app.extraction.schema import ExtractionOutput, FieldEvidence, InvoiceData, LineItem, ValidationIssue
from app.extraction.validation import ValidationConfig, validate

KEY_FIELDS = ("vendor", "invoice_number", "date", "total")
MONEY_FIELDS = ("subtotal", "discount", "taxable_amount", "tax", "service_charge", "other_charges", "total",
                "amount_paid", "amount_due")
TEXT_FIELDS = {"vendor": 200, "invoice_number": 64, "document_number": 64, "supplier_address": 300, "supplier_phone": 40,
               "supplier_email": 120, "supplier_tax_id": 40, "customer_name": 200, "customer_address": 300,
               "customer_id": 64, "customer_tax_id": 40}
DATE_FIELDS = ("date", "due_date")
SCORED = ("vendor", "invoice_number", "document_number", "date", "due_date", "currency", "language", "document_type",
          *MONEY_FIELDS, "supplier_address", "supplier_phone", "supplier_email", "supplier_tax_id", "customer_name",
          "customer_address", "customer_id", "customer_tax_id")
RISKY_FACTOR = 0.6       # value needed an OCR character substitution: trust it less
_DOC_TYPES = {"invoice", "receipt", "credit_note", "other"}


def _evidence(rf: RawField | None, n: Normalized | None, value, conf: float) -> FieldEvidence:
    return FieldEvidence(
        raw=rf.value if rf else None, value=None if value is None else str(value),
        transforms=list(n.transforms) if n else [], page=rf.page if rf else None,
        bbox=list(rf.bbox) if rf and rf.bbox else None, source_text=(rf.source_text[:300] if rf and rf.source_text else None),
        method=rf.method if rf else None, ocr_confidence=rf.ocr_confidence if rf else None, heuristic_confidence=conf)


def build_output(raw: RawExtraction, *, day_first: bool, review_threshold: float, today: date | None = None,
                 ocr_quality: float = 1.0, cfg: ValidationConfig | None = None) -> ExtractionOutput:
    today = today or date.today()
    cfg = cfg or ValidationConfig()
    issues: list[ValidationIssue] = []
    conf: dict[str, float] = {}
    ev: dict[str, FieldEvidence] = {}
    values: dict[str, object] = {}

    def f(name: str) -> RawField | None:
        return raw.fields.get(name)

    def strength(name: str, n: Normalized | None, has: bool) -> float:
        rf = f(name)
        c = (rf.confidence if rf else 0.0) * ocr_quality
        if n is not None and n.risky:
            c *= RISKY_FACTOR
        return round(min(1.0, c), 3) if has else 0.0

    # ---- dates (AD and BS)
    bs_text = None
    for name in DATE_FIELDS:
        rf = f(name)
        n = normalize_date(rf.value if rf else None, day_first=day_first, today=today) if rf and rf.value else None
        val = n.value if n else None
        values[name] = val
        if n is not None and n.calendar == "BS" and n.bs and name == "date":
            bs_text = f"{n.bs[0]:04d}-{n.bs[1]:02d}-{n.bs[2]:02d}"
        conf[name] = strength(name, n, val is not None)
        if n is not None:
            if n.ambiguous:
                issues.append(ValidationIssue(code="date_ambiguous", field=name, message="Date could be day-first or month-first"))
                conf[name] = round(conf[name] * 0.7, 3)
            if val is None:
                code = {"bs_conversion_unavailable": "bs_conversion_unavailable"}.get(n.note or "", "date_unparseable")
                issues.append(ValidationIssue(code=code, field=name, message="Date text could not be converted to a valid date"))
            elif n.risky:
                issues.append(ValidationIssue(code="date_ocr_repaired", field=name, message="Date needed an OCR character substitution"))
            ev[name] = _evidence(rf, n, val, conf[name])

    # ---- money
    for name in MONEY_FIELDS:
        rf = f(name)
        n = normalize_money(rf.value) if rf and rf.value else None
        val = n.value if n else None
        values[name] = val
        conf[name] = strength(name, n, val is not None)
        if n is not None:
            if val is None:
                issues.append(ValidationIssue(code="amount_unparseable", field=name, message="Amount text could not be parsed"))
            elif n.risky:
                issues.append(ValidationIssue(code="amount_ocr_repaired", field=name, message="Amount needed an OCR character substitution"))
            ev[name] = _evidence(rf, n, val, conf[name])

    # ---- text-like fields
    for name, limit in TEXT_FIELDS.items():
        rf = f(name)
        n = normalize_text(rf.value, limit) if rf and rf.value else None
        values[name] = n.value if n else None
        conf[name] = strength(name, n, values[name] is not None)
        if n is not None and n.value is not None:
            ev[name] = _evidence(rf, n, n.value, conf[name])

    cur_rf = f("currency")
    cur = normalize_currency(cur_rf.value if cur_rf else None)
    conf["currency"] = strength("currency", None, cur is not None)
    if cur_rf and cur:
        ev["currency"] = _evidence(cur_rf, None, cur, conf["currency"])
    lang_rf = f("language")
    lang = (lang_rf.value or "").lower() if lang_rf and lang_rf.value and re.fullmatch(r"[A-Za-z]{2,3}", lang_rf.value) else None
    conf["language"] = strength("language", None, lang is not None)
    dt_rf = f("document_type")
    dtype = (dt_rf.value or "").lower() if dt_rf and dt_rf.value else None
    dtype = dtype if dtype in _DOC_TYPES else None
    conf["document_type"] = strength("document_type", None, dtype is not None)

    items: list[LineItem] = []
    item_ev: list[dict] = []
    for it in raw.items:
        nq, nu, nd_, nt, na = (normalize_quantity(it.quantity), normalize_money(it.unit_price), normalize_money(it.discount),
                               normalize_money(it.tax), normalize_money(it.amount))
        items.append(LineItem(description=normalize_text(it.description).value, quantity=nq.value,
                              unit=normalize_text(it.unit, 20).value, unit_price=nu.value, discount=nd_.value,
                              tax=nt.value, amount=na.value))
        item_ev.append({"page": it.page, "bbox": list(it.bbox) if it.bbox else None,
                        "source_text": (it.source_text or "")[:300] or None,
                        "transforms": sorted({t for n in (nq, nu, nd_, nt, na) for t in n.transforms}),
                        "heuristic_confidence": round(min(1.0, it.confidence * ocr_quality), 3)})

    data = InvoiceData(
        document_type=dtype, date=values["date"], date_bs=bs_text, due_date=values["due_date"], currency=cur,  # type: ignore[arg-type]
        language=lang, items=items,
        **{k: values[k] for k in (*MONEY_FIELDS, *TEXT_FIELDS)})  # type: ignore[arg-type]

    issues.extend(validate(data, today=today, cfg=cfg))

    present = [conf[k] for k in KEY_FIELDS]  # missing key fields count as 0
    overall = sum(present) / len(present)
    overall *= 0.8 ** sum(1 for i in issues if i.severity == "error")
    overall *= 0.95 ** sum(1 for i in issues if i.severity == "warning")
    overall = round(max(0.0, min(1.0, overall)), 3)
    review = overall < review_threshold or any(i.severity == "error" for i in issues)
    conf = {k: v for k, v in conf.items() if k in SCORED and (v > 0 or k in ("vendor", "invoice_number", "date", "currency", "subtotal", "tax", "total"))}
    return ExtractionOutput(data=data, field_confidence=conf, evidence=ev, item_evidence=item_ev, confidence=overall,
                            requires_review=review, issues=issues)
