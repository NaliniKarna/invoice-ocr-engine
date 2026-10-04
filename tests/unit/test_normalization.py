"""Normalization with provenance, Gregorian and Bikram Sambat dates. BS anchors were checked against published
calendar conversions: 2082-04-01 BS = 2025-07-17 AD, 2082-06-15 BS = 2025-10-01 AD (approximately Ashwin mid-month)."""
from datetime import date
from decimal import Decimal

import pytest

from app.extraction.normalization import normalize_date, normalize_money, normalize_quantity

TODAY = date(2026, 10, 1)


def d(s, **kw):
    return normalize_date(s, today=TODAY, **kw)


# ------------------------------------------------------------------ dates: Gregorian
@pytest.mark.parametrize("raw,expected", [
    ("15/03/2026", date(2026, 3, 15)), ("2026-03-15", date(2026, 3, 15)), ("15.03.26", date(2026, 3, 15)),
    ("15-03-2026", date(2026, 3, 15)), ("15 Mar 2026", date(2026, 3, 15)), ("March 15, 2026", date(2026, 3, 15)),
    ("2026/3/5", date(2026, 3, 5)), ("15/03/2026 10:30", date(2026, 3, 15)), ("15/03/2026 10:30 PM", date(2026, 3, 15)),
])
def test_gregorian_formats_and_separators(raw, expected):
    r = d(raw)
    assert r.value == expected and r.calendar in ("AD", None) and r.bs is None


def test_ambiguous_day_month_is_flagged_and_follows_configuration():
    assert d("05/06/2026", day_first=True).value == date(2026, 6, 5) and d("05/06/2026").ambiguous
    assert d("05/06/2026", day_first=False).value == date(2026, 5, 6)
    assert not d("15/03/2026").ambiguous and not d("05/05/2026").ambiguous       # no ambiguity when both readings agree / impossible


def test_month_first_is_used_only_when_day_first_reading_is_impossible():
    assert d("03/15/2026", day_first=True).value == date(2026, 3, 15)


@pytest.mark.parametrize("raw", ["31/02/2026", "32/01/2026", "00/01/2026", "abc", "", "  ", "2026-13-40"])
def test_invalid_gregorian_dates_return_none_never_a_guess(raw):
    assert d(raw).value is None


def test_time_removal_is_recorded_as_provenance():
    assert "time_removed" in d("15/03/2026 10:30").transforms


# ------------------------------------------------------------------ dates: OCR corruption
def test_ocr_letter_for_digit_is_repaired_and_marked_risky():
    r = d("2O26-O3-15")
    assert r.value == date(2026, 3, 15) and r.risky and "ocr_substitution" in r.transforms and r.raw == "2O26-O3-15"


def test_ocr_repair_not_applied_to_words():
    assert d("Oct Olly").value is None
    assert d("lol").value is None


# ------------------------------------------------------------------ dates: Bikram Sambat
def test_bs_anchor_dates_convert_correctly():
    assert d("BS 2082-04-01").value == date(2025, 7, 17)
    assert d("2082/06/15 BS").value == date(2025, 10, 1)
    r = d("मिति 2082/06/15")
    assert r.value == date(2025, 10, 1) and r.calendar == "BS" and r.bs == (2082, 6, 15)


def test_bs_detected_by_year_without_marker_and_gregorian_untouched():
    assert d("2083/06/15").calendar == "BS" and d("2083/06/15").value == date(2026, 10, 1)
    assert d("2026-03-15").calendar == "AD" and d("2026-03-15").value == date(2026, 3, 15)    # Gregorian is never reinterpreted


def test_bs_devanagari_digits():
    r = d("मिति २०८२/०६/१५")
    assert r.value == date(2025, 10, 1) and "ascii_digits" in r.transforms


@pytest.mark.parametrize("raw", ["2082/02/32", "2082-13-01", "2082/00/10"])
def test_invalid_bs_dates_are_kept_unconverted(raw):
    r = d(raw)
    assert r.value is None and r.calendar == "BS" and r.note == "invalid_bs_date"


def test_year_beyond_bs_calendar_range_is_not_treated_as_bs():
    r = d("2150/01/01")
    assert r.calendar == "AD" and r.bs is None       # stays Gregorian; validation then rejects the implausible future date


# ------------------------------------------------------------------ money
@pytest.mark.parametrize("raw,expected", [
    ("Rs. 1,23,456.50", "123456.50"), ("1,234.50", "1234.50"), ("1.234,56", "1234.56"), ("(500.00)", "-500.00"),
    ("500.00-", "-500.00"), ("१२,३४५.००", "12345.00"), ("$ 9.99", "9.99"), ("12.345.678,90", "12345678.90"),
    ("1 234,50", "1234.50"), ("NPR 4746", "4746.00"),
])
def test_money_formats(raw, expected):
    assert normalize_money(raw).value == Decimal(expected)


def test_money_provenance_and_risk():
    r = normalize_money("NPR 12O.50")
    assert r.value == Decimal("120.50") and r.risky and "ocr_substitution" in r.transforms and "currency_marker_removed" in r.transforms
    assert r.raw == "NPR 12O.50"


def test_money_does_not_repair_non_numeric_words():
    assert normalize_money("Total").value is None and normalize_money("lol").value is None


def test_money_never_rounds_silently():
    r = normalize_money("10.505")
    assert r.value == Decimal("10.50") and "rounded_to_2dp" in r.transforms and r.risky


def test_money_ambiguous_three_digit_group_is_flagged_but_interpreted_consistently():
    a, b = normalize_money("1,500"), normalize_money("1.500")
    assert a.value == Decimal("1500.00") and a.ambiguous
    assert b.ambiguous and "ambiguous_separator" in b.transforms
    assert not normalize_money("1,500.00").ambiguous


@pytest.mark.parametrize("raw", ["", "abc", "1-2-3", "1.2.3"])
def test_money_garbage_is_none(raw):
    assert normalize_money(raw).value is None


def test_money_out_of_range():
    assert normalize_money("9" * 20).value is None


# ------------------------------------------------------------------ quantity
@pytest.mark.parametrize("raw,expected", [("2", "2"), ("1,5", "1.5"), ("2,25", "2.25"), ("1,500", "1500"), ("१०", "10"), ("0.75", "0.75")])
def test_quantity(raw, expected):
    assert normalize_quantity(raw).value == Decimal(expected)


def test_quantity_comma_is_not_silently_thousands():
    assert normalize_quantity("1,5").value == Decimal("1.5") and "decimal_comma" in normalize_quantity("1,5").transforms
    assert normalize_quantity("1,500").ambiguous


def test_quantity_garbage():
    assert normalize_quantity("x").value is None and normalize_quantity("2 kg").value is None
