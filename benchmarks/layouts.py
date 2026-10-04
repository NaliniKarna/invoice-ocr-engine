"""Diverse SYNTHETIC invoice layouts with exact word boxes ("oracle OCR").

Purpose: separate extraction failures from OCR failures. Each page is rendered by Chromium; the word boxes come from the
DOM, so an extractor fed these boxes sees perfect OCR. The same image can also go through a real OCR engine. The gap between
the two runs is the OCR loss; what stays wrong under perfect OCR is an extraction/layout failure.

Layouts 1-6 are the DEV split (extractor work may look at their failures). Layouts 7-12 are HELD OUT: they use different
labels, column names and geometry, and nobody tunes on them. These are synthetic documents drawn by the same author as
the extractor. Scores say how extractors rank on these layouts. They are NOT real-world accuracy.

    python -m benchmarks.layouts --out /tmp/layouts
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import zlib
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

DEV = (1, 2, 3, 4, 5, 6)
HELD_OUT = (7, 8, 9, 10, 11, 12)
# Round 2 (layouts 13-18) was written AFTER the extractor work was frozen and is run once: no extractor change was made after seeing
# its failures. It is the least biased view this synthetic setup can give. Run it with --round2.
ROUND2 = (13, 14, 15, 16, 17, 18)

_VENDORS = ["Harbor & Pine Supply Co.", "Brightwater Studio LLC", "Northfield Auto Parts", "Kestrel Print Works",
            "Lumen Office Solutions", "Redwood Catering Ltd", "Tandem Cycles Inc.", "Marlow & Finch Consulting",
            "Zenith Hardware Traders", "Orchid Lane Florists", "Delta Freight Services", "Quartz Software GmbH"]
_ITEMS = ["Steel bracket 40mm", "Printer toner cartridge", "Consulting - design review", "Delivery charge",
          "Office chair, ergonomic", "Annual software licence", "Copper pipe 15mm x 3m", "Catering - lunch for 12",
          "Bicycle inner tube", "Flower arrangement, large", "Pallet transport", "Web hosting (12 months)",
          "Cable tie pack", "Whiteboard markers", "Site survey visit", "LED panel light"]
_DATE_FORMATS = ["%d %B %Y", "%Y-%m-%d", "%b %d, %Y", "%d/%m/%Y", "%d-%b-%Y", "%B %d, %Y"]


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _doc(rng: random.Random, *, currency="USD", sym="$", eu=False, tax_rate=10, inclusive=False, n_items=None) -> dict:
    items = []
    for desc in rng.sample(_ITEMS, n_items or rng.randint(3, 5)):
        qty = rng.randint(1, 9)
        price = _q(Decimal(rng.randint(500, 45000)) / 100)
        items.append({"desc": desc, "qty": qty, "price": price, "amount": _q(price * qty)})
    sub = sum((i["amount"] for i in items), Decimal(0))
    if inclusive:
        total = sub
        tax = _q(total * tax_rate / (100 + tax_rate))
    else:
        tax = _q(sub * tax_rate / 100)
        total = sub + tax
    date = dt.date(2025, rng.randint(1, 12), rng.randint(13, 28))
    return {"vendor": rng.choice(_VENDORS), "inv": f"{rng.choice(['INV', 'BL', 'SO', 'A'])}-{rng.randint(1000, 99999)}",
            "date": date, "items": items, "sub": sub, "tax": tax, "total": total, "rate": tax_rate, "cur": currency,
            "sym": sym, "eu": eu, "customer": rng.choice(["Maple Dental Clinic", "R. Ostrowski", "Greenfield School Board", "Hana Takeda"]),
            "inclusive": inclusive}


def money(d: dict, x: Decimal, symbol=True) -> str:
    s = f"{x:,.2f}"
    if d["eu"]:
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    if not symbol:
        return s
    return f"{s} {d['sym']}" if d["eu"] else f"{d['sym']}{s}"


def dstr(d: dict, fmt: str) -> str:
    return d["date"].strftime(fmt)


_CSS = """*{box-sizing:border-box}body{margin:0;background:#fff;color:#111;font-family:'Liberation Sans',Arial,sans-serif;font-size:14px}
.pg{padding:36px}table{border-collapse:collapse;width:100%}th{text-align:left;font-weight:700;padding:6px 8px;background:#eee}
td{padding:6px 8px;border-bottom:1px solid #ddd}.r{text-align:right}.c{text-align:center}.mut{color:#666;font-size:12px}
.tot td{border:none;padding:3px 8px}.big{font-size:30px;font-weight:700}.row{display:flex;justify-content:space-between;gap:24px}
.grid{display:grid;gap:12px}.card{border:1px solid #bbb;padding:8px 12px}.b{font-weight:700}"""


def _rows(d, cols) -> str:
    out = []
    for it in d["items"]:
        out.append("<tr>" + "".join(f"<td class='{c.get('cls', '')}'>{c['f'](d, it)}</td>" for c in cols) + "</tr>")
    return "".join(out)


def _head(cols) -> str:
    return "<tr>" + "".join(f"<th class='{c.get('cls', '')}'>{c['h']}</th>" for c in cols) + "</tr>"


def _col(h, f, cls=""):
    return {"h": h, "f": f, "cls": cls}


def _totals(d, rows, cls="r", width=None) -> str:
    w = f"style='width:{width}px;margin-left:auto'" if width else ""
    return "<table class='tot' " + w + ">" + "".join(
        f"<tr><td class='{cls}'>{'<b>' + k + '</b>' if bold else k}</td><td class='r'>{'<b>' + v + '</b>' if bold else v}</td></tr>"
        for k, v, bold in rows) + "</table>"


# ----------------------------------------------------------------------------------------------------- layouts
def L1(d, rng):   # classic: vendor left, title + meta right, bill-to, 4-col table, totals right
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Description", lambda d, i: i["desc"]), _col("Qty", lambda d, i: i["qty"], "r"),
            _col("Unit Price", lambda d, i: money(d, i["price"]), "r"), _col("Amount", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div class=row><div><div class=big>{d['vendor']}</div><div class=mut>12 Mill Road, Springfield</div></div>
<div style='text-align:right'><div class=big style='color:#888'>INVOICE</div><div>Invoice No: {d['inv']}</div><div>Date: {dstr(d, f)}</div></div></div>
<p><b>Bill To</b><br>{d['customer']}<br>44 Elm Street</p><table>{_head(cols)}{_rows(d, cols)}</table><br>
{_totals(d, [("Subtotal", money(d, d['sub']), 0), (f"VAT ({d['rate']}%)", money(d, d['tax']), 0), ("Total", money(d, d['total']), 1)], width=300)}</div>"""


def L2(d, rng):   # logo+vendor, key/values right, leading Date column, "Taxes", plural labels
    f = "%d/%m/%Y"
    cols = [_col("Date", lambda d, i: dstr(d, f)), _col("Description", lambda d, i: i["desc"]),
            _col("Qty", lambda d, i: i["qty"], "r"), _col("Rate", lambda d, i: money(d, i["price"]), "r"),
            _col("Amount", lambda d, i: money(d, i["amount"]), "r")]
    return 900, f"""<div class=pg><div class=row><div style='display:flex;gap:14px;align-items:center'><div style='width:70px;height:70px;border-radius:35px;background:#6a4;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px'>LOGO</div>
<div class=b style='font-size:22px'>{d['vendor']}</div></div>
<div style='text-align:right'><div>Invoice # {d['inv']}</div><div>Invoice Date {dstr(d, f)}</div><div>Terms Net 30</div></div></div>
<p class=mut>Customer: {d['customer']}</p><table>{_head(cols)}{_rows(d, cols)}</table><br>
{_totals(d, [("Sub Total", money(d, d['sub']), 0), ("Taxes", money(d, d['tax']), 0), ("Total", money(d, d['total']), 1)], width=280)}</div>"""


def L3(d, rng):   # label-above-value cards, "Sales Tax", "Total Due"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Item", lambda d, i: i["desc"]), _col("Hrs", lambda d, i: i["qty"], "r"), _col("Rate", lambda d, i: money(d, i["price"]), "r"),
            _col("Line total", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div class=b style='font-size:24px;margin-bottom:18px'>{d['vendor']}</div>
<div class=grid style='grid-template-columns:repeat(3,1fr)'><div class=card><div class=mut>INVOICE NUMBER</div><div class=b>{d['inv']}</div></div>
<div class=card><div class=mut>ISSUED</div><div class=b>{dstr(d, f)}</div></div><div class=card><div class=mut>BILLED TO</div><div class=b>{d['customer']}</div></div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Sub-total", money(d, d['sub']), 0), (f"Sales Tax ({d['rate']}%)", money(d, d['tax']), 0), ("Total Due", money(d, d['total']), 1)], width=300)}</div>"""


def L4(d, rng):   # narrow thermal receipt, centered, item lines "name qty x price amount"
    f = rng.choice(["%d-%b-%Y", "%Y-%m-%d"])
    lines = "".join(f"<div class=row><span>{i['desc'][:22]}</span><span>{i['qty']} x {money(d, i['price'], False)}</span><span>{money(d, i['amount'], False)}</span></div>" for i in d["items"])
    return 460, f"""<div class=pg style="font-family:'DejaVu Sans Mono',monospace;font-size:13px"><div class=c><div class=b style='font-size:20px'>{d['vendor']}</div><div>Thank you for shopping</div></div>
<hr><div class=row><span>Receipt No: {d['inv']}</span><span>{dstr(d, f)}</span></div><hr>{lines}<hr>
<div class=row><span>SUBTOTAL</span><span>{money(d, d['sub'], False)}</span></div><div class=row><span>GST {d['rate']}%</span><span>{money(d, d['tax'], False)}</span></div>
<div class=row b><b>TOTAL</b><b>{money(d, d['total'], False)}</b></div></div>"""


def L5(d, rng):   # sidebar, SKU + Disc% + Tax% columns, European number format, IVA, Grand Total
    f = "%d.%m.%Y"
    cols = [_col("SKU", lambda d, i: "K" + str(zlib.crc32(i["desc"].encode()) % 9000 + 1000)), _col("Product", lambda d, i: i["desc"]),
            _col("Qty", lambda d, i: i["qty"], "r"), _col("Price", lambda d, i: money(d, i["price"], False), "r"),
            _col("Disc %", lambda d, i: "0", "r"), _col("Tax %", lambda d, i: f"{d['rate']}", "r"), _col("Total", lambda d, i: money(d, i["amount"], False), "r")]
    return 1000, f"""<div style='display:flex'><div style='width:230px;background:#223;color:#fff;padding:30px 18px;min-height:700px'>
<div class=b style='font-size:20px'>{d['vendor']}</div><p class=mut style='color:#aab'>Calle Mayor 5<br>28013 Madrid</p><p><b>Cliente</b><br>{d['customer']}</p></div>
<div style='flex:1;padding:30px'><div class=row><div class=big>Factura</div><div style='text-align:right'>Factura N.º {d['inv']}<br>Fecha {dstr(d, f)}</div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Base imponible", money(d, d['sub']), 0), (f"IVA {d['rate']}%", money(d, d['tax']), 0), ("Total factura", money(d, d['total']), 1)], width=320)}</div></div>"""


def L6(d, rng):   # totals bottom-left, bank block right, "Total excl./incl. VAT"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Item description", lambda d, i: i["desc"]), _col("Quantity", lambda d, i: i["qty"], "c"),
            _col("Price", lambda d, i: money(d, i["price"]), "r"), _col("Amount", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div class=big>{d['vendor']}</div><div class=mut>VAT Reg GB 123 4567 89</div><br>
<div class=row><div><b>Invoice</b> {d['inv']}<br>Date: {dstr(d, f)}</div><div>To: {d['customer']}</div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br><div class=row><div style='width:300px'>{_totals(d, [("Total excl. VAT", money(d, d['sub']), 0), (f"VAT {d['rate']}%", money(d, d['tax']), 0), ("Total incl. VAT", money(d, d['total']), 1)], cls='')}</div>
<div class=mut>Bank: First Union<br>IBAN GB29 NWBK 6016 1331 9268 19</div></div></div>"""


def L7(d, rng):   # held-out: Particulars / Nos / Rate / Value, GST, Total Amount After Tax
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("S.No", lambda d, i: d["items"].index(i) + 1), _col("Particulars", lambda d, i: i["desc"]), _col("Nos", lambda d, i: i["qty"], "r"),
            _col("Rate", lambda d, i: money(d, i["price"], False), "r"), _col("Value", lambda d, i: money(d, i["amount"], False), "r")]
    return 820, f"""<div class=pg><div class=c><div class=b style='font-size:24px'>{d['vendor']}</div><div class=mut>GSTIN 29ABCDE1234F1Z5</div><div class=b>TAX INVOICE</div></div><br>
<div class=row><div>Bill No.: {d['inv']}</div><div>Dated: {dstr(d, f)}</div></div><br><table>{_head(cols)}{_rows(d, cols)}</table><br>
{_totals(d, [("Total Amount Before Tax", money(d, d['sub'], False), 0), (f"GST {d['rate']}%", money(d, d['tax'], False), 0), ("Total Amount After Tax", "₹ " + money(d, d['total'], False), 1)], width=400)}</div>"""


def L8(d, rng):   # held-out: amount-due banner first, vendor in smaller type, "Date of issue"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Service", lambda d, i: i["desc"]), _col("Unit cost", lambda d, i: money(d, i["price"]), "r"), _col("Qty", lambda d, i: i["qty"], "r"),
            _col("Amount", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div style='background:#1a3;color:#fff;padding:16px 22px'><div class=mut style='color:#cfc'>Amount due</div><div class=big>{money(d, d['total'])}</div></div><br>
<div class=row><div><div class=b>{d['vendor']}</div><div class=mut>Remit to accounts@example.test</div></div><div>Invoice number<br><b>{d['inv']}</b></div><div>Date of issue<br><b>{dstr(d, f)}</b></div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Subtotal", money(d, d['sub']), 0), ("Tax", money(d, d['tax']), 0), ("Total", money(d, d['total']), 1)], width=260)}</div>"""


def L9(d, rng):   # held-out: two-line items, "Invoice Date", "VAT (15%)", "Amount payable"
    f = rng.choice(_DATE_FORMATS)
    body = "".join(f"<tr><td>{n + 1}</td><td>{i['desc']}<div class=mut>Ref. {rng.randint(100, 999)} - standard terms</div></td><td class=r>{i['qty']}</td><td>pcs</td><td class=r>{money(d, i['amount'])}</td></tr>" for n, i in enumerate(d["items"]))
    return 820, f"""<div class=pg><div class=row><div class=b style='font-size:22px'>{d['vendor']}</div><div class=big style='color:#a33'>INVOICE</div></div><br>
<table style='width:420px'><tr><td>Invoice Number</td><td class=b>{d['inv']}</td></tr><tr><td>Invoice Date</td><td class=b>{dstr(d, f)}</td></tr><tr><td>Payment Terms</td><td>30 days</td></tr></table><br>
<table><tr><th>#</th><th>Description</th><th class=r>Qty</th><th>Unit</th><th class=r>Amount</th></tr>{body}</table><br>
{_totals(d, [("Subtotal", money(d, d['sub']), 0), (f"VAT (15%)", money(d, d['tax']), 0), ("Amount payable", money(d, d['total']), 1)], width=320)}</div>"""


def L10(d, rng):  # held-out: tax-inclusive prices, only a total plus "of which VAT"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Article", lambda d, i: i["desc"]), _col("Units", lambda d, i: i["qty"], "r"), _col("Price", lambda d, i: money(d, i["price"]), "r"),
            _col("Sum", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div class=b style='font-size:26px'>{d['vendor']}</div><div>Invoice No. {d['inv']} &nbsp; | &nbsp; {dstr(d, f)}</div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Invoice total (prices include tax)", money(d, d['total']), 1), (f"of which VAT {d['rate']}%", money(d, d['tax']), 0)], width=420)}</div>"""


def L11(d, rng):  # held-out: logo + vendor smaller than the INVOICE title, key/value 2-col table, "Invoice Ref"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Description", lambda d, i: i["desc"]), _col("Qty", lambda d, i: i["qty"], "r"), _col("Price", lambda d, i: money(d, i["price"]), "r"),
            _col("Total", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div class=big style='font-size:44px;letter-spacing:6px'>INVOICE</div><div style='display:flex;gap:10px;align-items:center;margin:6px 0'>
<div style='width:34px;height:34px;background:#47a'></div><div class=b style='font-size:16px'>{d['vendor']}</div></div>
<table style='width:380px'><tr><td class=mut>Invoice Ref</td><td>{d['inv']}</td></tr><tr><td class=mut>Date</td><td>{dstr(d, f)}</td></tr><tr><td class=mut>Client</td><td>{d['customer']}</td></tr></table><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Net total", money(d, d['sub']), 0), ("Sales tax", money(d, d['tax']), 0), ("Invoice total", money(d, d['total']), 1)], width=300)}</div>"""


def L12(d, rng):  # held-out: wide GST table, many columns, "Taxable Value / Total Tax / Invoice Total"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Code", lambda d, i: "P" + str(zlib.crc32(i["desc"].encode()) % 900 + 100)), _col("Description", lambda d, i: i["desc"]), _col("HSN", lambda d, i: "8473"),
            _col("Qty", lambda d, i: i["qty"], "r"), _col("UoM", lambda d, i: "Nos"), _col("Rate", lambda d, i: money(d, i["price"], False), "r"),
            _col("Taxable", lambda d, i: money(d, i["amount"], False), "r"), _col("GST%", lambda d, i: d["rate"], "r"),
            _col("Amount", lambda d, i: money(d, _q(i["amount"] * (100 + d["rate"]) / 100), False), "r")]
    return 1200, f"""<div class=pg><div class=row><div class=b style='font-size:24px'>{d['vendor']}</div><div>Inv No: {d['inv']}<br>Inv Date: {dstr(d, f)}</div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Taxable Value", "₹ " + money(d, d['sub'], False), 0), ("Total Tax", "₹ " + money(d, d['tax'], False), 0), ("Invoice Total", "₹ " + money(d, d['total'], False), 1)], width=360)}</div>"""


def L13(d, rng):  # Indian GST bill: CGST + SGST printed separately, "Sl", "Amt", "Grand Total"
    f = rng.choice(["%d/%m/%Y", "%d-%b-%Y"])
    cols = [_col("Sl", lambda d, i: d["items"].index(i) + 1), _col("Item", lambda d, i: i["desc"]), _col("HSN", lambda d, i: "9983"),
            _col("Qty", lambda d, i: i["qty"], "r"), _col("Rate", lambda d, i: money(d, i["price"], False), "r"), _col("Amt", lambda d, i: money(d, i["amount"], False), "r")]
    half = _q(d["tax"] / 2)
    return 860, f"""<div class=pg><div class=row><div><div class=b style='font-size:22px'>{d['vendor']}</div><div class=mut>GSTIN: 27AAPFU0939F1ZV</div></div><div style='text-align:right'><b>TAX INVOICE</b><br>Bill Number: {d['inv']}<br>Bill Date: {dstr(d, f)}</div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Sub Total", money(d, d['sub'], False), 0), (f"CGST {d['rate'] / 2:g}%", money(d, half, False), 0), (f"SGST {d['rate'] / 2:g}%", money(d, d['tax'] - half, False), 0), ("Grand Total", "₹ " + money(d, d['total'], False), 1)], width=300)}</div>"""


def L14(d, rng):  # consulting: hours/fee, "Less: Discount", "VAT 20%", "Amount Due"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Project", lambda d, i: i["desc"]), _col("Hours", lambda d, i: i["qty"], "r"), _col("Rate", lambda d, i: money(d, i["price"]), "r"), _col("Fee", lambda d, i: money(d, i["amount"]), "r")]
    disc = _q(d["sub"] * Decimal("0.05")); taxable = d["sub"] - disc
    d["tax"] = _q(taxable * d["rate"] / 100); d["total"] = taxable + d["tax"]
    return 820, f"""<div class=pg><div class=big>{d['vendor']}</div><br><div class=row><div>Invoice #: {d['inv']}<br>Issue date: {dstr(d, f)}</div><div>Prepared for<br><b>{d['customer']}</b></div></div><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Subtotal", money(d, d['sub']), 0), ("Less: Discount 5%", "-" + money(d, disc), 0), (f"VAT {d['rate']}%", money(d, d['tax']), 0), ("Amount Due", money(d, d['total']), 1)], width=320)}</div>"""


def L15(d, rng):  # German: Rechnung Nr., Rechnungsdatum, Nettobetrag, MwSt, Gesamtbetrag (vocabulary outside the lexicon)
    f = "%d.%m.%Y"
    cols = [_col("Pos", lambda d, i: d["items"].index(i) + 1), _col("Bezeichnung", lambda d, i: i["desc"]), _col("Menge", lambda d, i: i["qty"], "r"),
            _col("Einzelpreis", lambda d, i: money(d, i["price"]), "r"), _col("Gesamt", lambda d, i: money(d, i["amount"]), "r")]
    return 860, f"""<div class=pg><div class=b style='font-size:24px'>{d['vendor']}</div><div class=mut>Hauptstraße 12, 10115 Berlin</div><br><div class=big style='font-size:24px'>Rechnung</div>
<div>Rechnung Nr. {d['inv']}<br>Rechnungsdatum: {dstr(d, f)}</div><br><table>{_head(cols)}{_rows(d, cols)}</table><br>
{_totals(d, [("Nettobetrag", money(d, d['sub']), 0), (f"MwSt {d['rate']}%", money(d, d['tax']), 0), ("Gesamtbetrag", money(d, d['total']), 1)], width=320)}</div>"""


def L16(d, rng):  # section header rows inside the table (no amounts) and a 3-line vendor block
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Description", lambda d, i: i["desc"]), _col("Qty", lambda d, i: i["qty"], "r"), _col("Unit Price", lambda d, i: money(d, i["price"]), "r"), _col("Amount", lambda d, i: money(d, i["amount"]), "r")]
    half = max(1, len(d["items"]) // 2)
    body = "<tr><td colspan=4 class=b style='background:#f4f4f4'>Goods</td></tr>" + "".join(
        f"<tr><td>{i['desc']}</td><td class=r>{i['qty']}</td><td class=r>{money(d, i['price'])}</td><td class=r>{money(d, i['amount'])}</td></tr>" for i in d["items"][:half]) + \
        "<tr><td colspan=4 class=b style='background:#f4f4f4'>Services</td></tr>" + "".join(
        f"<tr><td>{i['desc']}</td><td class=r>{i['qty']}</td><td class=r>{money(d, i['price'])}</td><td class=r>{money(d, i['amount'])}</td></tr>" for i in d["items"][half:])
    return 820, f"""<div class=pg><div class=b style='font-size:20px'>{d['vendor']}</div><div class=mut>Unit 4, Mill Lane Trading Estate<br>Leeds LS1 4AB &nbsp; Tel 0113 496 0000</div><br>
<div class=row><div><b>INVOICE {d['inv']}</b></div><div>{dstr(d, f)}</div></div><br><table>{_head(cols)}{body}</table><br>
{_totals(d, [("Net", money(d, d['sub']), 0), (f"VAT @ {d['rate']}%", money(d, d['tax']), 0), ("Total", money(d, d['total']), 1)], width=260)}</div>"""


def L17(d, rng):  # summary box with the totals ABOVE the line items
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Product", lambda d, i: i["desc"]), _col("Qty", lambda d, i: i["qty"], "r"), _col("Price", lambda d, i: money(d, i["price"]), "r"), _col("Total", lambda d, i: money(d, i["amount"]), "r")]
    return 820, f"""<div class=pg><div class=row><div class=b style='font-size:24px'>{d['vendor']}</div><div>Order confirmation / invoice<br>No. {d['inv']}<br>{dstr(d, f)}</div></div><br>
<div class=card style='width:320px;margin-left:auto'><div class=row><span>Subtotal</span><span>{money(d, d['sub'])}</span></div><div class=row><span>Tax</span><span>{money(d, d['tax'])}</span></div>
<div class=row><b>Invoice Total</b><b>{money(d, d['total'])}</b></div></div><br><table>{_head(cols)}{_rows(d, cols)}</table></div>"""


def L18(d, rng):  # vendor name in a coloured band (white on dark), meta in a bordered 2-column grid, "Total Payable"
    f = rng.choice(_DATE_FORMATS)
    cols = [_col("Description", lambda d, i: i["desc"]), _col("Quantity", lambda d, i: i["qty"], "r"), _col("Unit price", lambda d, i: money(d, i["price"]), "r"), _col("Net amount", lambda d, i: money(d, i["amount"]), "r")]
    return 840, f"""<div style='background:#234;color:#fff;padding:18px 36px'><div class=b style='font-size:24px'>{d['vendor']}</div></div><div class=pg>
<table style='width:360px;margin-left:auto;border:1px solid #888'><tr><td style='border:1px solid #888'>Invoice no.</td><td style='border:1px solid #888'>{d['inv']}</td></tr>
<tr><td style='border:1px solid #888'>Invoice date</td><td style='border:1px solid #888'>{dstr(d, f)}</td></tr><tr><td style='border:1px solid #888'>Customer</td><td style='border:1px solid #888'>{d['customer']}</td></tr></table><br>
<table>{_head(cols)}{_rows(d, cols)}</table><br>{_totals(d, [("Total net", money(d, d['sub']), 0), (f"Value added tax {d['rate']}%", money(d, d['tax']), 0), ("Total payable", money(d, d['total']), 1)], width=360)}</div>"""


LAYOUTS = {1: (L1, dict(currency="USD", sym="$", tax_rate=10)), 2: (L2, dict(currency="USD", sym="$", tax_rate=8)),
           3: (L3, dict(currency="USD", sym="$", tax_rate=7)), 4: (L4, dict(currency="INR", sym="₹", tax_rate=5)),
           5: (L5, dict(currency="EUR", sym="€", eu=True, tax_rate=21)), 6: (L6, dict(currency="GBP", sym="£", tax_rate=20)),
           7: (L7, dict(currency="INR", sym="₹", tax_rate=18)), 8: (L8, dict(currency="USD", sym="$", tax_rate=10)),
           9: (L9, dict(currency="EUR", sym="€", tax_rate=15)), 13: (L13, dict(currency="INR", sym="₹", tax_rate=18)), 14: (L14, dict(currency="GBP", sym="£", tax_rate=20)),
           15: (L15, dict(currency="EUR", sym="€", eu=True, tax_rate=19)), 16: (L16, dict(currency="GBP", sym="£", tax_rate=20)), 17: (L17, dict(currency="USD", sym="$", tax_rate=9)),
           18: (L18, dict(currency="EUR", sym="€", tax_rate=21)), 10: (L10, dict(currency="GBP", sym="£", tax_rate=20, inclusive=True)),
           11: (L11, dict(currency="USD", sym="$", tax_rate=6)), 12: (L12, dict(currency="INR", sym="₹", tax_rate=18))}

_WORDS_JS = """() => { const out=[]; const w=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
 let n; while((n=w.nextNode())){ const t=n.textContent; const re=/\\S+/g; let m; while((m=re.exec(t))){ const r=document.createRange();
 r.setStart(n,m.index); r.setEnd(n,m.index+m[0].length); const b=r.getBoundingClientRect(); if(b.width>0) out.push([m[0],b.left,b.top,b.right,b.bottom]); } } return out; }"""


def truth_of(d: dict, layout: int) -> dict:
    t = {"vendor": d["vendor"], "invoice_number": d["inv"], "date": d["date"].isoformat(), "subtotal": str(d["sub"]),
         "tax": str(d["tax"]), "total": str(d["total"]), "currency": d["cur"], "n_items": len(d["items"]),
         "item_amounts": sorted(str(i["amount"]) for i in d["items"])}
    if layout == 4:
        t["currency"] = None            # a till receipt that prints no currency sign: nothing to read
    if d["inclusive"]:
        t["subtotal"] = None            # never printed on the page, so not scored
    return t


def generate(per_layout: int = 3, seed: int = 11, scale: int = 2, out: Path | None = None, layouts: tuple[int, ...] | None = None):
    """Yield dicts: id, layout, split, png bytes, words (page pixels), width, height, truth."""
    from playwright.sync_api import sync_playwright
    docs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for lid, (fn, kw) in LAYOUTS.items():
            if layouts is not None and lid not in layouts or layouts is None and lid in ROUND2:
                continue
            for k in range(per_layout):
                rng = random.Random(seed * 1000 + lid * 37 + k)
                d = _doc(rng, **kw)
                width, body = fn(d, rng)
                page = browser.new_page(viewport={"width": width, "height": 600}, device_scale_factor=scale)
                page.set_content(f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>{body}</body></html>")
                words = [[w[0], *(round(v * scale) for v in w[1:])] for w in page.evaluate(_WORDS_JS)]
                png = page.screenshot(full_page=True)
                from PIL import Image
                import io
                im = Image.open(io.BytesIO(png))
                docs.append({"id": f"L{lid:02d}-{k}", "layout": lid, "split": "dev" if lid in DEV else "held_out" if lid in HELD_OUT else "round2", "png": png,
                             "words": words, "width": im.width, "height": im.height, "truth": truth_of(d, lid)})
                page.close()
        browser.close()
    if out:
        out.mkdir(parents=True, exist_ok=True)
        for x in docs:
            (out / f"{x['id']}.png").write_bytes(x["png"])
            (out / f"{x['id']}.json").write_text(json.dumps({k: v for k, v in x.items() if k != "png"}))
    return docs


def oracle_ocr(doc: dict):
    """Exact word boxes as an OCRResult (perfect OCR): words are grouped into lines by vertical overlap."""
    from app.ocr.base import OCRLine, OCRPage, OCRResult, OCRWord
    ws = sorted(doc["words"], key=lambda w: ((w[2] + w[4]) / 2, w[1]))
    lines: list[list] = []
    for w in ws:
        cy = (w[2] + w[4]) / 2
        if lines and abs(cy - lines[-1][0]) < 0.5 * (w[4] - w[2]):
            lines[-1][1].append(w)
        else:
            lines.append([cy, [w]])
    ocr_lines = []
    for _, group in lines:
        group.sort(key=lambda w: w[1])
        words = [OCRWord(w[0], 1.0, (w[1], w[2], w[3], w[4])) for w in group]
        ocr_lines.append(OCRLine(" ".join(w.text for w in words), 1.0,
                                 (min(w.bbox[0] for w in words), min(w.bbox[1] for w in words), max(w.bbox[2] for w in words), max(w.bbox[3] for w in words)), words))
    return OCRResult("oracle", "dom-1", [OCRPage(1, doc["width"], doc["height"], ocr_lines)])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--per-layout", type=int, default=3)
    a = ap.parse_args()
    ds = generate(a.per_layout, out=a.out)
    print(len(ds), "documents ->", a.out)
