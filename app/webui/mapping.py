"""Pure translation between the Bill OCR API and what a contributor sees. No I/O.

Three jobs: (1) plain-language states and "please check" messages instead of statuses and issue codes, (2) the list of
fields a contributor can edit, (3) turning the submitted form into the minimal set of corrections by comparing it with the
ORIGINAL extraction fetched from the API (never with anything the browser claims about the original)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.extraction.normalize import parse_money, parse_quantity

# ------------------------------------------------------------------ what can be edited
# key, label, kind (text|date|money|currency), group. Keys are the API's correctable field names.
FIELDS: list[tuple[str, str, str, str]] = [
    ("vendor", "Supplier", "text", "invoice"),
    ("invoice_number", "Invoice number", "text", "invoice"),
    ("date", "Date", "date", "invoice"),
    ("currency", "Currency", "currency", "amounts"),
    ("subtotal", "Subtotal", "money", "amounts"),
    ("tax", "Tax", "money", "amounts"),
    ("total", "Total", "money", "amounts"),
    ("document_number", "Document number", "text", "more"),
    ("due_date", "Due date", "date", "more"),
    ("discount", "Discount", "money", "more"),
    ("taxable_amount", "Taxable amount", "money", "more"),
    ("service_charge", "Service charge", "money", "more"),
    ("other_charges", "Other charges", "money", "more"),
    ("amount_paid", "Amount paid", "money", "more"),
    ("amount_due", "Amount due", "money", "more"),
    ("supplier_address", "Supplier address", "text", "more"),
    ("supplier_phone", "Supplier phone", "text", "more"),
    ("supplier_email", "Supplier email", "text", "more"),
    ("supplier_tax_id", "Supplier tax number", "text", "more"),
    ("customer_name", "Customer", "text", "more"),
    ("customer_address", "Customer address", "text", "more"),
    ("customer_id", "Customer ID", "text", "more"),
    ("customer_tax_id", "Customer tax number", "text", "more"),
]
KIND = {k: kind for k, _, kind, _ in FIELDS}
ITEM_COLUMNS: list[tuple[str, str, str]] = [("description", "Description", "text"), ("quantity", "Qty", "qty"),
                                            ("unit_price", "Unit price", "money"), ("amount", "Amount", "money")]
ITEM_KIND = {k: kind for k, _, kind in ITEM_COLUMNS}


def field_spec() -> dict[str, Any]:
    return {"fields": [{"key": k, "label": label, "kind": kind, "group": g} for k, label, kind, g in FIELDS],
            "item_columns": [{"key": k, "label": label, "kind": kind} for k, label, kind in ITEM_COLUMNS]}


# ------------------------------------------------------------------ states
def ui_state(status: str, reviewed: bool) -> str:
    """reading | ready | reviewed | failed"""
    if status in ("queued", "processing"):
        return "reading"
    if status == "completed":
        return "reviewed" if reviewed else "ready"
    return "failed"


# ------------------------------------------------------------------ "please check" messages
_ISSUE_TEXT = {
    "missing_vendor": "We couldn't find the supplier name.",
    "suspicious_vendor": "This may not be the supplier name.",
    "missing_invoice_number": "We couldn't find an invoice number.",
    "suspicious_invoice_number": "This may not be an invoice number.",
    "missing_date": "We couldn't find the date.",
    "date_out_of_range": "This date looks unlikely.",
    "due_date_before_date": "The due date is before the invoice date.",
    "missing_total": "We couldn't find the total.",
    "totals_mismatch": "The amounts don't add up to the total.",
    "amount_due_mismatch": "Amount due doesn't match total minus amount paid.",
    "overpaid": "The amount paid is higher than the total.",
    "missing_tax": "Tax may be missing.",
    "missing_subtotal": "The subtotal may be missing.",
    "items_without_amounts": "The items have no amounts.",
    "items_sum_mismatch": "The items don't add up to the subtotal.",
    "negative_quantity": "The quantity is negative.",
    "line_amount_mismatch": "Quantity × unit price doesn't match the amount.",
    "missing_currency": "We couldn't tell the currency.",
    "invalid_email": "This email address looks wrong.",
    "tax_id_format": "This tax number looks wrong.",
    "unsupported_script": "This invoice may be in a script we can't read reliably.",
}
GENERIC_CHECK = "Please check this value."


def flags(issues: list[dict] | None) -> dict[str, str]:
    """Issue list -> {field or items[n] or items or _general: one short sentence}. Codes, severities and the API's own
    wording are never passed on."""
    out: dict[str, list[str]] = {}
    for i in issues or []:
        text = _ISSUE_TEXT.get(i.get("code", ""))
        field = i.get("field")
        if text is None:
            if not field:
                continue
            text = GENERIC_CHECK
        key = field if field else "_general"
        if text not in out.setdefault(key, []):
            out[key].append(text)
    return {k: " ".join(v) for k, v in out.items()}


# ------------------------------------------------------------------ detail payload
def _num_text(v: Any, kind: str) -> str | None:
    if v is None:
        return None
    d = Decimal(str(v))
    if kind == "money" and d == d.quantize(Decimal("0.01")):
        return f"{d:.2f}"
    return format(d.normalize(), "f") if kind == "qty" else format(d, "f")


def _value_text(v: Any, kind: str) -> str | None:
    if v is None:
        return None
    if kind in ("money", "qty"):
        return _num_text(v, kind)
    return str(v)


def detail_payload(d: dict[str, Any], token: str, next_token: str | None) -> dict[str, Any]:
    reviewed = bool(d.get("reviewed"))
    state = ui_state(d.get("status", ""), reviewed)
    out: dict[str, Any] = {"id": token, "name": d.get("filename") or "Invoice", "state": state, "reviewed": reviewed, "next": next_token}
    if state in ("ready", "reviewed"):
        res = d.get("result") or {}
        out["values"] = {k: _value_text(res.get(k), kind) for k, _, kind, _ in FIELDS}
        out["items"] = [{k: _value_text(row.get(k), kind) for k, _, kind in ITEM_COLUMNS} for row in res.get("items") or []]
        out["flags"] = flags(d.get("issues")) if state == "ready" else {}
    return out


# ------------------------------------------------------------------ submitted form -> corrections
@dataclass
class Built:
    corrections: list[dict]
    errors: dict[str, str]          # field key (or items[n].col) -> message shown next to it


_WS = re.compile(r"\s+")


def _text(v: Any) -> str | None:
    if v is None:
        return None
    s = _WS.sub(" ", str(v)).strip()
    return s or None


def _canon(kind: str, raw: Any) -> tuple[Any, str | None]:
    """Normalise a submitted value. Returns (comparable/sendable value or None, error message)."""
    t = _text(raw)
    if t is None:
        return None, None
    if kind == "text":
        return (t, None) if len(t) <= 200 else (None, "This is too long.")
    if kind == "currency":
        return (t.upper(), None) if re.fullmatch(r"[A-Za-z]{3}", t) else (None, "Use a 3-letter currency code, such as USD.")
    if kind == "date":
        try:
            return date.fromisoformat(t).isoformat(), None
        except ValueError:
            return None, "Enter a valid date."
    d = parse_quantity(t) if kind == "qty" else parse_money(t)
    if d is None or abs(d) > 10**12:
        return None, "Enter a number."
    return d, None


def _original(kind: str, v: Any) -> Any:
    if v is None:
        return None
    if kind in ("money", "qty"):
        return parse_quantity(str(v)) if kind == "qty" else parse_money(str(v))
    if kind == "currency":
        return str(v).upper()
    return _text(v) if kind == "text" else str(v)


def _same(a: Any, b: Any) -> bool:
    return a == b


def _send(kind: str, value: Any) -> Any:
    return None if value is None else (str(value) if kind in ("money", "qty") else value)


def build_corrections(original: dict[str, Any], submitted: dict[str, Any]) -> Built:
    """`original` = the API's stored result; `submitted` = {"values": {key: str|None}, "items": [{col: str|None}]}.
    Only fields that really differ are returned. Cleared fields become `null` ("should be empty")."""
    corrections: list[dict] = []
    errors: dict[str, str] = {}
    values = submitted.get("values") or {}
    for key, _, kind, _ in FIELDS:
        if key not in values:
            continue
        new, err = _canon(kind, values[key])
        if err:
            errors[key] = err
            continue
        if not _same(new, _original(kind, original.get(key))):
            corrections.append({"field": key, "value": _send(kind, new)})

    orig_items = original.get("items") or []
    rows = submitted.get("items") or []
    if len(rows) > len(orig_items) + 1:
        errors["items"] = "You can add one item at a time."
        rows = rows[: len(orig_items) + 1]
    for n, row in enumerate(rows):
        existing = orig_items[n] if n < len(orig_items) else None
        for col, _, kind in ITEM_COLUMNS:
            if col not in row:
                continue
            new, err = _canon(kind, row[col])
            name = f"items[{n}].{col}"
            if err:
                errors[name] = err
                continue
            if existing is None:
                if new is not None:                                       # a row the extraction missed: add what was typed
                    corrections.append({"field": name, "value": _send(kind, new)})
            elif not _same(new, _original(kind, existing.get(col))):
                corrections.append({"field": name, "value": _send(kind, new)})
    return Built(corrections, errors)
