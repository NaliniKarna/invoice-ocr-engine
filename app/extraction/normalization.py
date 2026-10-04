"""Normalization with provenance.

Every function returns a `Normalized` that carries the raw text, the parsed value and the list of transformations
that were applied on the way (for example Devanagari digits converted, a letter O read as zero). A value that needed
a risky transformation (OCR character substitution) is marked so confidence scoring and review logic can see it.
Nothing here edits source evidence: the raw text is always kept next to the normalized value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

NORMALIZATION_VERSION = "1"

_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
_ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# Characters OCR engines commonly confuse with digits. Applied ONLY inside tokens that are otherwise numeric.
_OCR_DIGIT = {"O": "0", "o": "0", "l": "1", "I": "1", "|": "1"}
_BS_MARKERS = re.compile(r"(?i)(?:\bB\.?\s?S\.?\b|वि\.?\s?सं\.?|विक्रम\s*सम्वत|बि\.?\s?सं\.?|\bmiti\b|मिति)")
_CURRENCY_WORDS = re.compile(r"(?i)\b(?:npr|nrs|rs|inr|usd|eur|gbp|aud|cad|sgd|aed|jpy|cny)\b\.?|रू\.?|रु\.?|[$€£₹]")
BS_MIN_YEAR, BS_MAX_YEAR = 1970, 2099   # range covered by the calendar library used for conversion


@dataclass
class Normalized:
    raw: str | None
    value: Any = None
    transforms: list[str] = field(default_factory=list)
    risky: bool = False                 # an OCR-substitution (or similar guess) was needed to get this value
    ambiguous: bool = False
    note: str | None = None             # machine-readable reason when value is None or uncertain

    @property
    def changed(self) -> bool:
        return bool(self.transforms)


def to_ascii_digits(s: str) -> tuple[str, bool]:
    out = s.translate(_DEVANAGARI).translate(_ARABIC_INDIC)
    return out, out != s


# ------------------------------------------------------------------ text
def normalize_text(raw: str | None, max_len: int = 200) -> Normalized:
    if raw is None:
        return Normalized(None, None)
    v = re.sub(r"\s+", " ", raw).strip()
    tf = ["whitespace"] if v != raw else []
    v = v[:max_len] or None
    return Normalized(raw, v, tf)


# ------------------------------------------------------------------ numbers
def normalize_money(raw: str | None) -> Normalized:
    """Decimal with 2 places. Handles 1,234.50 / 1.234,50 / 1,23,456.00 (lakh) / (1,234.00) / 1234.00- / Devanagari digits."""
    if raw is None:
        return Normalized(None, None)
    s = str(raw).strip()
    tf: list[str] = []
    s, changed = to_ascii_digits(s)
    if changed:
        tf.append("ascii_digits")
    s2 = _CURRENCY_WORDS.sub(" ", s)
    if s2 != s:
        tf.append("currency_marker_removed"); s = s2
    negative = False
    if re.fullmatch(r"\s*\(.*\)\s*", s):
        negative = True; s = s.strip()[1:-1]; tf.append("parentheses_negative")
    if re.search(r"\d\s*-\s*$", s):
        negative = True; s = re.sub(r"-\s*$", "", s); tf.append("trailing_minus")
    risky = False
    token = s.strip()
    # OCR letter/digit confusion, only when the token is otherwise numeric and has at least two real digits.
    if re.search(r"[Ool|I]", token) and len(re.findall(r"\d", token)) >= 2 and re.fullmatch(r"[\d.,\-\sOol|I]+", token):
        fixed = "".join(_OCR_DIGIT.get(c, c) for c in token)
        tf.append("ocr_substitution"); risky = True; token = fixed
    cleaned = re.sub(r"[^\d.,\-]", "", token)
    if cleaned != token.replace(" ", ""):
        tf.append("non_numeric_removed")
    if not cleaned or not re.search(r"\d", cleaned):
        return Normalized(raw, None, tf, note="no_digits")
    if cleaned.count("-") > 1 or ("-" in cleaned and not cleaned.startswith("-")):
        return Normalized(raw, None, tf, note="malformed_sign")
    ambiguous = bool(re.fullmatch(r"-?\d{1,3}[.,]\d{3}", cleaned))     # "1,500" / "1.500": thousands or 3 decimals?
    if ambiguous:
        tf.append("ambiguous_separator")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "") if cleaned.rfind(".") > cleaned.rfind(",") else cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".") if re.search(r",\d{1,2}$", cleaned) and cleaned.count(",") == 1 else cleaned.replace(",", "")
    if cleaned.count(".") > 1:
        return Normalized(raw, None, tf, note="malformed_number")
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return Normalized(raw, None, tf, note="malformed_number")
    if negative:
        d = -abs(d)
    if abs(d) > Decimal("1e12"):
        return Normalized(raw, None, tf, note="out_of_range")
    q = d.quantize(Decimal("0.01"))
    if q != d:
        tf.append("rounded_to_2dp"); risky = True      # digits were dropped: never silent
    return Normalized(raw, q, tf, risky=risky, ambiguous=ambiguous)


def normalize_quantity(raw: str | None) -> Normalized:
    """Decimal, precision preserved. A lone comma is a decimal comma when 1-2 digits follow ("1,5" = 1.5), otherwise a
    thousands separator ("1,500" = 1500, flagged ambiguous because EU documents may mean 1.500)."""
    if raw is None:
        return Normalized(None, None)
    s, changed = to_ascii_digits(str(raw).strip())
    tf = ["ascii_digits"] if changed else []
    amb = False
    if re.fullmatch(r"-?\d+,\d{1,2}", s):
        s = s.replace(",", "."); tf.append("decimal_comma")
    else:
        amb = bool(re.fullmatch(r"-?\d{1,3}[.,]\d{3}", s))
        if amb:
            tf.append("ambiguous_separator")
        s = s.replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return Normalized(raw, None, tf, note="malformed_number")
    if abs(d) > Decimal("1e9"):
        return Normalized(raw, None, tf, note="out_of_range")
    return Normalized(raw, d, tf, ambiguous=amb)


# ------------------------------------------------------------------ dates
@dataclass
class DateResult(Normalized):
    calendar: str | None = None                 # "AD" | "BS"
    bs: tuple[int, int, int] | None = None      # (year, month, day) as written, when calendar == "BS"
    original_calendar_text: str | None = None


def _bs_to_ad(y: int, m: int, d: int) -> date | None:
    try:
        import nepali_datetime as nd
    except ImportError:  # optional dependency: without it BS dates are kept verbatim, never guessed
        raise
    return nd.date(y, m, d).to_datetime_date()


def normalize_date(raw: str | None, *, day_first: bool = True, today: date | None = None) -> DateResult:
    """Gregorian (AD) and Bikram Sambat (BS) dates. BS is converted with a verified calendar library; a BS date that
    cannot be converted is kept as written (calendar="BS", value=None) and is never guessed into a Gregorian date."""
    today = today or date.today()
    if not raw or not str(raw).strip():
        return DateResult(raw, None)
    s = str(raw).strip()
    tf: list[str] = []
    s, ch = to_ascii_digits(s)
    if ch:
        tf.append("ascii_digits")
    bs_marker = bool(_BS_MARKERS.search(s))
    if bs_marker:
        s = _BS_MARKERS.sub(" ", s).strip(" :-,."); tf.append("bs_marker_removed")
    s = re.sub(r"\s+", " ", s).strip()
    t = re.sub(r"(?i)[\sT]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]\.?M\.?)?$", "", s)   # trailing time of day is not part of the date
    if t != s:
        s = t; tf.append("time_removed")
    risky = False
    # OCR letter/digit confusion, only when the whole token is otherwise a numeric date (>= 4 real digits).
    if re.fullmatch(r"[\dOol|I\s./\-,]+", s) and re.search(r"[Ool|I]", s) and len(re.findall(r"\d", s)) >= 4:
        s = "".join(_OCR_DIGIT.get(c, c) for c in s)
        tf.append("ocr_substitution"); risky = True

    y = m = d = None
    year_first = False
    mm = re.fullmatch(r"(\d{4})\s*[-/. ]\s*(\d{1,2})\s*[-/. ]\s*(\d{1,2})", s)
    if mm:
        y, m, d, year_first = int(mm[1]), int(mm[2]), int(mm[3]), True
    else:
        mm = re.fullmatch(r"(\d{1,2})\s*[-/. ]\s*(\d{1,2})\s*[-/. ]\s*(\d{4}|\d{2})", s)
        if mm:
            a, b, yy = int(mm[1]), int(mm[2]), int(mm[3])
            if len(mm[3]) == 2:
                yy += 2000; tf.append("two_digit_year")
            y = yy
            d, m = (a, b) if day_first else (b, a)
            ambiguous = a <= 12 and b <= 12 and a != b
            if not _valid_gregorian(y, m, d) and _valid_gregorian(y, d, m):
                m, d, ambiguous = d, m, False
            return _finish(raw, s, tf, risky, y, m, d, bs_marker, today, ambiguous_dm=ambiguous)
        mm = re.fullmatch(r"(\d{1,2})(?:\s+|\s*[-/.]\s*)([A-Za-z]{3})[a-z]*\.?,?(?:\s+|\s*[-/.]\s*)(\d{4}|\d{2})", s)    # "14 March 2025", "24-Dec-2025", "24/Dec/25"
        if mm and mm[2].lower() in _MONTHS:
            yy = int(mm[3]) + (2000 if len(mm[3]) == 2 else 0)
            if len(mm[3]) == 2:
                tf.append("two_digit_year")
            return _finish(raw, s, tf, risky, yy, _MONTHS[mm[2].lower()], int(mm[1]), bs_marker, today)
        mm = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
        if mm and mm[1].lower() in _MONTHS:
            return _finish(raw, s, tf, risky, int(mm[3]), _MONTHS[mm[1].lower()], int(mm[2]), bs_marker, today)
        return DateResult(raw, None, tf, note="unrecognised_format")
    return _finish(raw, s, tf, risky, y, m, d, bs_marker, today)


def _valid_gregorian(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def _finish(raw, s, tf, risky, y, m, d, bs_marker, today, ambiguous_dm: bool = False) -> DateResult:
    # Calendar decision. A year beyond next year's AD year but within the BS range can only be BS (AD 2060 is the future).
    bs = bs_marker or (today.year + 1 < y <= BS_MAX_YEAR)
    if bs:
        if not (BS_MIN_YEAR <= y <= BS_MAX_YEAR):
            return DateResult(raw, None, tf, risky, note="bs_year_out_of_range", calendar="BS", bs=(y, m, d))
        try:
            ad = _bs_to_ad(y, m, d)
        except ImportError:
            return DateResult(raw, None, tf + ["bs_unconverted"], risky, note="bs_conversion_unavailable", calendar="BS", bs=(y, m, d))
        except (ValueError, TypeError):
            return DateResult(raw, None, tf, risky, note="invalid_bs_date", calendar="BS", bs=(y, m, d))
        return DateResult(raw, ad, tf + ["bs_to_ad"], risky, ambiguous=False, calendar="BS", bs=(y, m, d),
                          original_calendar_text=s)
    if not _valid_gregorian(y, m, d):
        return DateResult(raw, None, tf, risky, note="invalid_date")
    return DateResult(raw, date(y, m, d), tf, risky, ambiguous=ambiguous_dm, calendar="AD")


# ------------------------------------------------------------------ OCR text repair (applied to evidence comparisons only)
def strip_currency(s: str) -> str:
    return _CURRENCY_WORDS.sub(" ", s).strip()
