from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from app.extraction.normalization import normalize_date, normalize_money, normalize_quantity

_CURRENCY_PATTERNS = tuple((re.compile(p, re.I if ci else 0), iso) for p, iso, ci in (
    (r"(?<![A-Za-z])NPR(?![A-Za-z])", "NPR", True), ("रू", "NPR", False), (r"(?<![A-Za-z])Rs\.?(?![A-Za-z])", "NPR", True),
    (r"(?<![A-Za-z])USD(?![A-Za-z])", "USD", True), (r"\$", "USD", False), (r"(?<![A-Za-z])EUR(?![A-Za-z])", "EUR", True), (r"€(?!\s*\d+(?:[.,]\d+)?\s*%)", "EUR", False),
    (r"(?<![A-Za-z])INR(?![A-Za-z])", "INR", True), ("₹", "INR", False), (r"(?<![A-Za-z])GBP(?![A-Za-z])", "GBP", True), ("£", "GBP", False),
    # Other ISO codes: upper-case whole tokens only, so ordinary words ("cad", "aed") never match.
    (r"(?<![A-Za-z])AED(?![A-Za-z])", "AED", False), (r"(?<![A-Za-z])AUD(?![A-Za-z])", "AUD", False),
    (r"(?<![A-Za-z])CAD(?![A-Za-z])", "CAD", False), (r"(?<![A-Za-z])SGD(?![A-Za-z])", "SGD", False),
    (r"(?<![A-Za-z])JPY(?![A-Za-z])", "JPY", False), (r"(?<![A-Za-z])CNY(?![A-Za-z])", "CNY", False)))
_LABELLED_CURRENCY = re.compile(r"(?<![A-Za-z])currency\s*(?:code)?\s*[:\-]\s*([A-Za-z]{3})(?![A-Za-z])", re.I)
ISO_CURRENCIES = {"NPR", "USD", "EUR", "INR", "GBP", "AUD", "CAD", "SGD", "JPY", "CNY", "AED"}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_money(raw: str | None) -> Decimal | None:
    """Value-only convenience wrapper; use normalization.normalize_money when provenance matters."""
    return normalize_money(raw).value


def parse_quantity(raw: str | None) -> Decimal | None:
    return normalize_quantity(raw).value


def detect_currency(text: str) -> str | None:
    """First currency marker found. ISO codes and "Rs" must be whole tokens: a substring match mislabelled
    any text containing e.g. "...RS " (BUYERS, SUPPLIERS) as NPR."""
    m = _LABELLED_CURRENCY.search(text)       # an explicit "Currency: AED" line wins over symbols elsewhere
    if m and m.group(1).upper() in ISO_CURRENCIES:
        return m.group(1).upper()
    for pattern, iso in _CURRENCY_PATTERNS:
        if pattern.search(text):
            return iso
    return None


def normalize_currency(raw: str | None) -> str | None:
    if raw is None:
        return None
    v = raw.strip().upper()
    return v if v in ISO_CURRENCIES else None


def parse_date(raw: str | None, *, day_first: bool = True) -> tuple[date | None, bool]:
    """Returns (Gregorian date, ambiguous). BS dates are converted; see normalization.normalize_date for provenance."""
    r = normalize_date(raw, day_first=day_first)
    return r.value, r.ambiguous


def clean_text(raw: str | None, max_len: int = 200) -> str | None:
    if raw is None:
        return None
    v = re.sub(r"\s+", " ", raw).strip()
    return v[:max_len] or None
