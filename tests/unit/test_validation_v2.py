from datetime import date
from decimal import Decimal as D

from app.extraction.schema import InvoiceData, LineItem
from app.extraction.validation import ValidationConfig, validate

TODAY = date(2026, 10, 1)


def codes(**kw):
    kw.setdefault("currency", "NPR")
    return {(i.code, i.severity) for i in validate(InvoiceData(**kw), today=TODAY)}


def test_consistent_invoice_has_no_issues():
    assert codes(vendor="A", date=date(2026, 9, 1), subtotal=D("1000"), tax=D("130"), total=D("1130")) == set()


def test_totals_mismatch_is_error():
    assert ("totals_mismatch", "error") in codes(subtotal=D("1000"), tax=D("130"), total=D("1140"))


def test_rounding_within_tolerance_is_accepted_and_real_errors_are_not():
    # default: max(0.02 absolute, 0.05% of the larger amount) = 0.565 here
    assert ("totals_mismatch", "error") not in codes(subtotal=D("1000.00"), tax=D("130.00"), total=D("1130.02"))
    assert ("totals_mismatch", "error") not in codes(subtotal=D("1000.00"), tax=D("130.00"), total=D("1130.50"))
    assert ("totals_mismatch", "error") in codes(subtotal=D("1000.00"), tax=D("130.00"), total=D("1131.00"))
    assert ("totals_mismatch", "error") in codes(subtotal=D("10.00"), tax=D("1.30"), total=D("11.40"))     # small invoice: abs tolerance rules


def test_relative_tolerance_scales_with_magnitude():
    cfg = ValidationConfig()
    big = InvoiceData(currency="NPR", subtotal=D("10000000"), tax=D("1300000"), total=D("11300400"))   # 0.0035% off
    assert not [i for i in validate(big, today=TODAY, cfg=cfg) if i.code == "totals_mismatch"]
    strict = ValidationConfig(tolerance_rel=D("0"))
    assert [i for i in validate(big, today=TODAY, cfg=strict) if i.code == "totals_mismatch"]


def test_tolerance_is_configurable():
    inv = InvoiceData(currency="NPR", subtotal=D("100"), tax=D("13"), total=D("113.50"))
    assert [i for i in validate(inv, today=TODAY) if i.code == "totals_mismatch"]
    assert not [i for i in validate(inv, today=TODAY, cfg=ValidationConfig(tolerance_abs=D("1"))) if i.code == "totals_mismatch"]


def test_discount_and_charges_enter_the_equation():
    assert ("totals_mismatch", "error") not in codes(subtotal=D("1000"), discount=D("100"), tax=D("117"),
                                                    service_charge=D("50"), total=D("1067"))
    # discount already removed from subtotal (accounting variant) is also accepted
    assert ("totals_mismatch", "error") not in codes(subtotal=D("900"), discount=D("100"), tax=D("117"), total=D("1017"))


def test_taxable_amount_path():
    assert ("totals_mismatch", "error") not in codes(taxable_amount=D("1000"), tax=D("130"), total=D("1130"))
    assert ("totals_mismatch", "error") in codes(taxable_amount=D("1000"), tax=D("130"), total=D("1200"))


def test_no_arithmetic_check_without_the_needed_fields():
    assert ("totals_mismatch", "error") not in codes(total=D("5"), subtotal=D("100"))      # tax absent: nothing to conclude


def test_amount_due_and_overpayment():
    assert ("amount_due_mismatch", "error") in codes(total=D("1000"), amount_paid=D("400"), amount_due=D("500"))
    assert ("overpaid", "warning") in codes(total=D("1000"), amount_paid=D("1200"))


def test_line_items_sum_and_line_amount():
    items = [LineItem(description="a", quantity=D("2"), unit_price=D("10"), amount=D("20")),
             LineItem(description="b", quantity=D("1"), unit_price=D("5"), amount=D("9"))]
    c = codes(items=items, subtotal=D("25"))
    assert ("items_sum_mismatch", "warning") in c and ("line_amount_mismatch", "warning") in c


def test_line_discount_and_line_tax_variants_accepted():
    assert ("line_amount_mismatch", "warning") not in codes(items=[LineItem(quantity=D("2"), unit_price=D("10"), discount=D("2"), amount=D("18"))])
    assert ("line_amount_mismatch", "warning") not in codes(items=[LineItem(quantity=D("2"), unit_price=D("10"), tax=D("2.6"), amount=D("22.6"))])


def test_negative_values_error_except_credit_notes():
    assert ("negative_total", "error") in codes(total=D("-5"))
    assert ("negative_total", "error") not in codes(total=D("-5"), document_type="credit_note")


def test_date_plausibility_and_due_date_order():
    assert ("date_out_of_range", "error") in codes(date=date(2031, 1, 1))
    assert ("date_out_of_range", "error") in codes(date=date(1999, 1, 1))
    assert ("due_date_before_date", "warning") in codes(date=date(2026, 5, 1), due_date=date(2026, 4, 1))


def test_formats_currency_email_nepal_pan():
    assert ("missing_currency", "warning") in {(i.code, i.severity) for i in validate(InvoiceData(total=D("1")), today=TODAY)}
    assert ("invalid_email", "warning") in codes(supplier_email="not-an-email")
    assert ("tax_id_format", "warning") in codes(supplier_tax_id="12345")
    assert ("tax_id_format", "warning") not in codes(supplier_tax_id="457-363-381")
    assert ("tax_id_format", "warning") not in codes(supplier_tax_id="12345", currency="USD")     # PAN rule is Nepal-specific


def test_absurd_amounts_flagged():
    assert ("amount_out_of_range", "error") in codes(total=D("1e13"))
    assert ("missing_total", "error") in codes(vendor="x")


def test_validation_never_mutates_the_input():
    inv = InvoiceData(currency="NPR", subtotal=D("1"), tax=D("1"), total=D("9"))
    before = inv.model_dump()
    validate(inv, today=TODAY)
    assert inv.model_dump() == before
