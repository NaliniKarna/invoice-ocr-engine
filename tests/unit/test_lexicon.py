"""Label lexicon: variable wording maps to roles without per-vendor rules."""
import pytest

from app.extraction.lexicon import date_strength, header_role, invoice_number_strength, money_roles


@pytest.mark.parametrize("label,role", [
    ("Tax", "tax"), ("Taxes", "tax"), ("VAT (15%)", "tax"), ("GST 5%", "tax"), ("IVA 21%", "tax"), ("Sales Tax (8%)", "tax"),
    ("Total Tax", "tax"), ("of which VAT 20%", "tax"),
    ("Subtotal", "subtotal"), ("Sub-total", "subtotal"), ("Sub Total", "subtotal"), ("Total excl. VAT", "subtotal"),
    ("Total Amount Before Tax", "subtotal"), ("Base imponible", "subtotal"), ("Subtotai", "subtotal"),
    ("Grand Total", "total"), ("Total Due", "total"), ("Total incl. VAT", "total"), ("Total Amount After Tax", "total"),
    ("Invoice total (prices include tax)", "total"), ("Total", "total"),
    ("Less: Discount 5%", "discount"), ("Delivery charge", "other"), ("Amount paid", "paid"),
])
def test_money_label_roles(label, role):
    roles = money_roles(label)
    assert roles and max(roles, key=roles.get) == role


def test_ambiguous_labels_keep_several_roles_for_arithmetic_to_settle():
    assert {"subtotal", "total"} <= set(money_roles("Net total"))
    assert {"total", "due"} <= set(money_roles("Amount payable"))


@pytest.mark.parametrize("label", ["TAX INVOICE", "Tax Registration No", "GSTIN", "Description", "Payment Terms", ""])
def test_labels_that_are_not_amounts_have_no_roles(label):
    assert money_roles(label) == {}


@pytest.mark.parametrize("label", ["Invoice No", "Invoice #", "INVOICE NUMBER", "Bill No.", "Receipt No", "Invoice Ref", "Factura N.º"])
def test_invoice_number_labels(label):
    assert invoice_number_strength(label) >= 2


@pytest.mark.parametrize("label", ["PO Number", "Customer No", "Account Number", "Phone", "Date"])
def test_other_numbers_are_not_the_invoice_number(label):
    assert invoice_number_strength(label) == 0


def test_date_labels():
    assert date_strength("Invoice Date")[0] == "date" and date_strength("Date of issue")[0] == "date" and date_strength("ISSUED")[0] == "date"
    assert date_strength("Due Date")[0] == "due_date"
    assert date_strength("Payment Terms")[0] == ""


def test_header_roles_are_hints_not_requirements():
    assert header_role("Unit Price")[0] == "unit_price" and header_role("Line total")[0] == "amount" and header_role("Qty")[0] == "quantity"
    assert header_role("Hrs")[0] == "quantity" and header_role("Particulars")[0] == "description"
    assert header_role("Zebra")[0] == ""
