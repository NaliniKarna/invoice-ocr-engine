"""Business-rule validation of an extracted invoice. Pure functions, no I/O.

Validation never changes a value. It only reports issues; `error` severity forces human review, `warning` lowers
confidence. Accounting practice varies (rounding per line, tax-inclusive lines, discounts before/after tax), so every
arithmetic check uses a tolerance and only runs when the fields it needs are present.

Tolerance (configurable, see Settings.validation_tolerance_abs / validation_tolerance_rel):
    allowed_difference = max(abs_tolerance, rel_tolerance * max(|reference amounts|))
Defaults: 0.02 currency units absolute (two rounding steps of 0.01) and 0.05% relative, which absorbs per-line
rounding on long invoices without hiding a wrong digit in the total.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.extraction.schema import InvoiceData, ValidationIssue

VALIDATION_VERSION = "3"
# Issues that mean "a key field is missing or looks wrong". The pipeline reads these to decide on another extraction strategy or
# another OCR pass even when the overall confidence is high (confidence averages four fields and hides a single bad one).
# Subset that triggers another extraction strategy / OCR pass. "missing_subtotal" and "missing_tax" are reported but do not trigger one:
# plenty of valid documents print neither.
GATE_CODES = frozenset({"missing_vendor", "missing_invoice_number", "missing_date", "missing_total", "suspicious_vendor", "suspicious_invoice_number",
                        "items_without_amounts", "totals_mismatch"})
KEY_FIELD_CODES = frozenset({"missing_vendor", "missing_invoice_number", "missing_date", "missing_total", "missing_subtotal", "missing_tax",
                             "suspicious_vendor", "suspicious_invoice_number", "items_without_amounts", "totals_mismatch"})
ZERO = Decimal(0)


@dataclass(frozen=True)
class ValidationConfig:
    tolerance_abs: Decimal = Decimal("0.02")
    tolerance_rel: Decimal = Decimal("0.0005")
    min_year: int = 2000
    max_money: Decimal = Decimal("1e12")
    # Required-field checks (missing/suspicious vendor, invoice number, date; implied but missing tax/subtotal). They judge an
    # EXTRACTION, so they are off for other callers of validate() (for example the check of a human-corrected training example,
    # where a partial truth is legitimate). The worker turns them on (Settings.validate_required_fields).
    required_fields: bool = False


def _tol(cfg: ValidationConfig, *refs: Decimal | None) -> Decimal:
    big = max((abs(r) for r in refs if r is not None), default=ZERO)
    return max(cfg.tolerance_abs, cfg.tolerance_rel * big)


def _v(x: Decimal | None) -> Decimal:
    return x if x is not None else ZERO


_LABEL_WORDS = frozenset({"invoice", "date", "total", "subtotal", "tax", "vat", "gst", "amount", "due", "balance", "description", "qty", "quantity", "price",
                          "bill", "to", "from", "customer", "receipt", "page", "number", "no", "payment", "terms", "item", "items", "ship", "paid"})


def _suspicious_vendor(v: str) -> bool:
    t = v.strip()
    letters = sum(c.isalpha() for c in t)
    if letters < 3 or letters / max(1, len(t.replace(" ", ""))) < 0.5:
        return True
    words = re.findall(r"[A-Za-z]+", t.lower())
    if words and all(w in _LABEL_WORDS for w in words):
        return True
    if re.search(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b", t) or re.search(r"[$€£₹]\s*\d", t):
        return True
    return len(t) > 80 or bool(re.match(r"^(invoice|date|total|amount|bill to|ship to|page)\b", t, re.I))


def _suspicious_number(n: str) -> bool:
    t = n.strip()
    if len(t) < 2 or not re.search(r"\d", t):
        return True
    return bool(re.fullmatch(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", t) or re.fullmatch(r"[\d,]+\.\d{2}", t))


def validate(data: InvoiceData, *, today: date | None = None, cfg: ValidationConfig = ValidationConfig()) -> list[ValidationIssue]:
    today = today or date.today()
    issues: list[ValidationIssue] = []

    def add(code: str, field: str | None, message: str, severity: str = "warning") -> None:
        issues.append(ValidationIssue(code=code, field=field, severity=severity, message=message))

    credit = data.document_type == "credit_note"

    # --- dates
    if data.date and (data.date > today + timedelta(days=1) or data.date.year < cfg.min_year):
        add("date_out_of_range", "date", "Date is implausible", "error")
    if data.due_date and data.date and data.due_date < data.date:
        add("due_date_before_date", "due_date", "Due date is before the document date")
    if data.due_date and data.due_date.year < cfg.min_year:
        add("date_out_of_range", "due_date", "Due date is implausible", "error")

    # --- single-amount sanity
    for name in ("subtotal", "taxable_amount", "tax", "service_charge", "other_charges", "total", "amount_paid", "amount_due"):
        val = getattr(data, name)
        if val is None:
            continue
        if abs(val) > cfg.max_money:
            add("amount_out_of_range", name, f"{name} is implausibly large", "error")
        elif val < 0 and not credit and name in ("subtotal", "total", "tax", "taxable_amount"):
            add("negative_total" if name == "total" else f"negative_{name}", name, f"{name} is negative", "error")
    if data.discount is not None and data.discount < 0:
        add("negative_discount", "discount", "Discount is negative")
    if data.total is None:
        add("missing_total", "total", "Total not found", "error")

    # --- required fields: a missing or implausible key field is reported even when everything else looks fine
    if cfg.required_fields:
        if not (data.vendor or "").strip():
            add("missing_vendor", "vendor", "Supplier name not found")
        elif _suspicious_vendor(data.vendor):
            add("suspicious_vendor", "vendor", "Supplier name looks like a date, an amount, a label or a document title")
        if not (data.invoice_number or "").strip():
            add("missing_invoice_number", "invoice_number", "Invoice number not found")
        elif _suspicious_number(data.invoice_number):
            add("suspicious_invoice_number", "invoice_number", "Invoice number looks like a date, an amount or a plain word")
        if data.date is None and data.total is not None:
            add("missing_date", "date", "Document date not found")

    # --- document-level arithmetic
    charges = _v(data.service_charge) + _v(data.other_charges)
    # Till receipts round the payable amount to a whole unit ("round off"): a printed whole-number total may differ from
    # the exact sum by less than 0.5. Applied only when the printed total has no fractional part.
    whole_total = data.total is not None and data.total == data.total.to_integral_value()

    def matches(expected: Decimal, *refs: Decimal | None) -> bool:
        diff = abs(expected - data.total)
        return diff <= _tol(cfg, data.total, *refs) or (whole_total and diff < Decimal("0.5"))

    items_explain_total = False
    if data.items and data.total is not None and data.tax is not None:
        # multi-section bills (food + liquor) print a subtotal for one section only; the items still add up to the total
        item_sum = sum((i.amount or ZERO) for i in data.items)
        items_explain_total = matches(item_sum - _v(data.discount) + data.tax + charges, item_sum)
    if data.subtotal is not None and data.total is not None and data.tax is not None:
        expected = data.subtotal - _v(data.discount) + data.tax + charges
        if not matches(expected, data.subtotal) and not items_explain_total:
            # tolerate the variant where the discount was already removed from the subtotal
            alt = data.subtotal + data.tax + charges
            if not matches(alt, data.subtotal):
                add("totals_mismatch", "total", "subtotal - discount + tax + charges does not equal total", "error")
    elif data.taxable_amount is not None and data.tax is not None and data.total is not None:
        if abs(data.taxable_amount + data.tax + charges - data.total) > _tol(cfg, data.total, data.taxable_amount):
            add("totals_mismatch", "total", "taxable amount + tax + charges does not equal total", "error")
    if data.total is not None and data.amount_paid is not None and data.amount_due is not None:
        if abs(data.total - data.amount_paid - data.amount_due) > _tol(cfg, data.total):
            add("amount_due_mismatch", "amount_due", "total - paid does not equal amount due", "error")
    if data.total is not None and data.amount_paid is not None and data.amount_paid > data.total + _tol(cfg, data.total):
        add("overpaid", "amount_paid", "Amount paid exceeds the total")

    # breakdown fields: only flagged when the document itself shows a breakdown (a lone total on a till receipt is normal)
    if cfg.required_fields and data.total is not None:
        implied_tax = data.subtotal is not None and data.total - data.subtotal - charges + _v(data.discount) > _tol(cfg, data.total)
        if data.tax is None and implied_tax:
            add("missing_tax", "tax", "Total is higher than the subtotal but no tax amount was found")
        if data.subtotal is None and data.tax is not None and (data.items or data.taxable_amount is not None):
            add("missing_subtotal", "subtotal", "Tax and total were found but no subtotal")
    if cfg.required_fields and data.items and not any(i.amount is not None for i in data.items):
        add("items_without_amounts", "items", "Line items were found but none has an amount")

    # --- line items
    if data.items and data.subtotal is not None:
        s = sum((i.amount or ZERO) for i in data.items)
        if abs(s - data.subtotal) > _tol(cfg, data.subtotal, s) and not items_explain_total:
            add("items_sum_mismatch", "items", "Line items do not sum to subtotal")
    for n, it in enumerate(data.items):
        if it.quantity is not None and it.quantity < 0 and not credit:
            add("negative_quantity", f"items[{n}]", "Quantity is negative")
        if it.quantity is not None and it.unit_price is not None and it.amount is not None:
            base = it.quantity * it.unit_price - _v(it.discount)
            ok = abs(base - it.amount) <= _tol(cfg, it.amount, base) or abs(base + _v(it.tax) - it.amount) <= _tol(cfg, it.amount, base)
            if not ok:
                add("line_amount_mismatch", f"items[{n}]", "qty x unit_price (less discount, optionally plus tax) != amount")

    # --- formats
    if data.currency is None:
        add("missing_currency", "currency", "Currency not detected")
    if data.supplier_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", data.supplier_email):
        add("invalid_email", "supplier_email", "Supplier email is malformed")
    for field in ("supplier_tax_id", "customer_tax_id"):
        val = getattr(data, field)
        if val and data.currency == "NPR" and not re.fullmatch(r"\d{9}", re.sub(r"[\s\-]", "", val)):
            add("tax_id_format", field, "Nepal PAN/VAT numbers have 9 digits")
    return issues
