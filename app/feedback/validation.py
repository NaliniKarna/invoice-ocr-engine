"""Validates client corrections against the stored result. Pure functions: no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.extraction.normalize import normalize_currency, parse_date, parse_money, parse_quantity
from app.extraction.schema import InvoiceData

_SCALAR = {"vendor": "text", "invoice_number": "text", "date": "date", "currency": "currency",
           "subtotal": "money", "tax": "money", "total": "money",
           # schema v2 (additive): every key above keeps its v1 meaning
           "document_number": "text", "due_date": "date", "supplier_address": "text", "supplier_phone": "text",
           "supplier_email": "text", "supplier_tax_id": "text", "customer_name": "text", "customer_address": "text",
           "customer_id": "text", "customer_tax_id": "text", "language": "text", "document_type": "text",
           "discount": "money", "taxable_amount": "money", "service_charge": "money", "other_charges": "money",
           "amount_paid": "money", "amount_due": "money"}
_ITEM_RE = re.compile(r"^items\[(\d{1,3})\]\.(description|quantity|unit|unit_price|discount|tax|amount)$")
_ITEM_KIND = {"description": "text", "quantity": "qty", "unit": "text", "unit_price": "money", "discount": "money",
              "tax": "money", "amount": "money"}


@dataclass
class ValidCorrection:
    field: str
    original: Any
    corrected: Any


def _canon(kind: str, value: Any) -> Any:
    """Canonical JSON-safe form. Raises ValueError if invalid."""
    if value is None:
        return None
    if kind == "text":
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError("expected string up to 200 chars")
        v = re.sub(r"\s+", " ", value).strip()
        if any(ord(c) < 32 for c in v):
            raise ValueError("control characters not allowed")
        return v or None
    if kind == "currency":
        c = normalize_currency(value if isinstance(value, str) else None)
        if not c:
            raise ValueError("unsupported currency code")
        return c
    if kind == "date":
        d, _ = parse_date(value if isinstance(value, str) else None, day_first=True)
        if d is None:
            raise ValueError("expected ISO date YYYY-MM-DD")
        return d.isoformat()
    if kind in ("money", "qty"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("expected number")
        d = parse_money(str(value)) if kind == "money" else parse_quantity(str(value))
        if d is None or abs(d) > 10**12:
            raise ValueError("invalid number")
        return str(d)
    raise ValueError("unknown kind")


def _original(data: dict, field: str) -> Any:
    m = _ITEM_RE.match(field)
    if m:
        idx, sub = int(m[1]), m[2]
        items = data.get("items") or []
        return items[idx].get(sub) if idx < len(items) else None
    return data.get(field)


def validate_corrections(corrections: list[tuple[str, Any]], stored: dict) -> tuple[list[ValidCorrection], list[dict]]:
    InvoiceData.model_validate(stored)  # stored result must itself be well-formed
    ok: list[ValidCorrection] = []
    errors: list[dict] = []
    seen: set[str] = set()
    n_items = len(stored.get("items") or [])
    for field, value in corrections:
        if field in seen:
            errors.append({"field": field, "code": "duplicate_field"}); continue
        seen.add(field)
        m = _ITEM_RE.match(field)
        kind = _SCALAR.get(field) or (_ITEM_KIND[m[2]] if m else None)
        if kind is None:
            errors.append({"field": field, "code": "unknown_field"}); continue
        if m and int(m[1]) > n_items:  # may correct existing rows or append exactly one past the end
            errors.append({"field": field, "code": "item_index_out_of_range"}); continue
        try:
            corrected = _canon(kind, value)
        except ValueError as exc:
            errors.append({"field": field, "code": "invalid_value", "message": str(exc)}); continue
        orig = _original(stored, field)
        if _same(kind, orig, corrected):
            errors.append({"field": field, "code": "no_change"}); continue
        ok.append(ValidCorrection(field, orig, corrected))
    return ok, errors


def _same(kind: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if kind in ("money", "qty"):
        return parse_money(str(a)) == parse_money(str(b))
    return str(a) == str(b)
