"""SYNTHETIC invoice generator for pipeline testing and benchmark plumbing.

Everything produced here is SYNTHETIC: rendered by this script from random data with a fixed seed. Results measured on it
show whether the pipeline and metrics work and how engines behave on clean, typeset, English-language layouts. They say
NOTHING about accuracy on real client invoices (real scans, photos, stamps, handwriting, Devanagari text, unusual
templates). Do not quote these numbers as real-world accuracy.

Deterministic: same (seed, n) -> byte-identical truth and identical pixel data for clean samples.
"""
from __future__ import annotations

import io
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
VENDORS = ["Himalayan Traders Pvt Ltd", "Everest Office Supplies", "Kathmandu Fresh Mart", "Annapurna Electronics",
           "Lumbini Hardware Store", "Pokhara Stationery House", "Bagmati Medical Hall", "Mustang Organic Foods"]
CUSTOMERS = ["Sagarmatha Builders", "Rara Lake Resort", "Koshi Cooperative", "Janaki Enterprises", "Gandaki Clinic"]
ITEMS = ["Rice 25kg", "Cooking Oil 5L", "A4 Paper Ream", "Ball Pen Box", "LED Bulb 9W", "Hand Sanitizer", "Notebook 200pg",
         "Steel Bucket", "Tea Leaves 1kg", "Wall Clock", "Printer Toner", "Extension Cord", "Floor Cleaner", "Sugar 10kg"]
STREETS = ["Baneshwor, Kathmandu", "Lakeside, Pokhara", "Butwal-5, Rupandehi", "Bhaktapur Durbar Marg", "Birgunj Main Road"]
TEMPLATES = ("standard", "receipt", "nepal")
DEGRADATIONS = ("clean", "noise", "blur", "rotate", "lowcontrast", "jpeg")


def money(d: Decimal) -> str:
    return f"{d:,.2f}"


@dataclass
class Sample:
    id: str
    template: str
    degradation: str
    group_key: str
    images: list[bytes]                    # one PNG per page
    truth: dict                            # schema-v2-shaped ground truth (strings for numbers/dates)
    ocr_text: str                          # exact rendered text, row by row (for CER/WER)
    synthetic: bool = True
    meta: dict = field(default_factory=dict)


def _font(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


class _Page:
    def __init__(self, w=1240, h=1754):
        self.img = Image.new("RGB", (w, h), "white")
        self.d = ImageDraw.Draw(self.img)
        self.w, self.h = w, h
        self.rows: list[str] = []

    def text(self, xy, s, size=26, bold=False, anchor="la"):
        self.d.text(xy, s, fill="black", font=_font(size, bold), anchor=anchor)

    def row(self, y, cells, size=26, bold=False):
        """cells: list of (x, text, anchor). Records the row's text in reading order."""
        for x, t, a in cells:
            self.text((x, y), t, size, bold, a)
        self.rows.append(" ".join(t for _, t, _ in sorted(cells, key=lambda c: c[0])))


def _degrade(img: Image.Image, kind: str, rng: random.Random) -> Image.Image:
    if kind == "clean":
        return img
    if kind == "noise":
        px = img.load()
        for _ in range(img.width * img.height // 120):
            x, y = rng.randrange(img.width), rng.randrange(img.height)
            v = rng.randrange(0, 255)
            px[x, y] = (v, v, v)
        return img
    if kind == "blur":
        return img.filter(ImageFilter.GaussianBlur(1.3))
    if kind == "rotate":
        return img.rotate(rng.uniform(-1.6, 1.6) or 0.8, expand=False, fillcolor="white", resample=Image.BICUBIC)
    if kind == "lowcontrast":
        return Image.eval(img, lambda v: 150 + v * 100 // 255)
    if kind == "jpeg":
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=22); return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    raise ValueError(kind)


def _items(rng: random.Random, n: int) -> list[dict]:
    out = []
    for name in rng.sample(ITEMS, n):
        q = Decimal(rng.randint(1, 12))
        p = Decimal(rng.randint(40, 4200)) + Decimal(rng.choice([0, 0, 0.5, 0.25, 0.75]))
        out.append({"description": name, "quantity": str(q), "unit_price": str(p.quantize(Decimal("0.01"))),
                    "amount": str((q * p).quantize(Decimal("0.01")))})
    return out


def _totals(items: list[dict], rng: random.Random, vat: Decimal, with_discount: bool) -> dict:
    sub = sum((Decimal(i["amount"]) for i in items), Decimal(0))
    disc = (sub * Decimal("0.05")).quantize(Decimal("0.01")) if with_discount else None
    taxable = sub - (disc or Decimal(0))
    tax = (taxable * vat).quantize(Decimal("0.01"))
    total = taxable + tax
    return {"subtotal": sub, "discount": disc, "taxable_amount": taxable if with_discount else None, "tax": tax, "total": total}


def make_sample(idx: int, seed: int, template: str, degradation: str, pages: int = 1) -> Sample:
    rng = random.Random(f"{seed}:{idx}")
    vendor = rng.choice(VENDORS)
    customer = rng.choice(CUSTOMERS)
    inv_no = f"INV-{rng.randint(2025, 2026)}-{rng.randint(1000, 9999)}"
    d0 = date(2026, 1, 1) + timedelta(days=rng.randint(0, 250))
    due = d0 + timedelta(days=rng.choice([7, 15, 30]))
    cur_label = rng.choice(["NPR", "Rs."]) if template != "nepal" else "Rs."
    n_items = rng.randint(3, 6) if pages == 1 else rng.randint(9, 11)
    items = _items(rng, n_items)
    tot = _totals(items, rng, Decimal("0.13"), with_discount=(template == "standard" and rng.random() < 0.4))
    pan = f"{rng.randint(10**8, 10**9 - 1)}"
    addr = rng.choice(STREETS)
    phone = f"01-{rng.randint(4000000, 4999999)}"

    truth: dict = {"document_type": "receipt" if template == "receipt" else "invoice", "invoice_number": inv_no,
                   "vendor": vendor, "currency": None,  # no currency marker is rendered on these templates, so no label is claimed "supplier_address": addr, "supplier_phone": phone,
                   "subtotal": str(tot["subtotal"]), "tax": str(tot["tax"]), "total": str(tot["total"]),
                   "items": items, "page_count": pages}
    if tot["discount"] is not None:
        truth["discount"] = str(tot["discount"])
        truth["taxable_amount"] = str(tot["taxable_amount"])
    bs_text = None
    if template == "nepal":
        import nepali_datetime as nd
        bs = nd.date.from_datetime_date(d0)
        bs_text = f"{bs.year:04d}/{bs.month:02d}/{bs.day:02d}"
        truth["date"] = d0.isoformat(); truth["date_bs"] = f"{bs.year:04d}-{bs.month:02d}-{bs.day:02d}"
        truth["supplier_tax_id"] = pan
    else:
        truth["date"] = d0.isoformat()
    if template == "standard":
        truth["due_date"] = due.isoformat(); truth["customer_name"] = customer

    all_rows: list[list[str]] = []
    imgs: list[Image.Image] = []
    per_page = len(items) if pages == 1 else (len(items) + pages - 1) // pages
    chunks = [items[i:i + per_page] for i in range(0, len(items), per_page)] or [[]]
    for pno, chunk in enumerate(chunks, start=1):
        pg = _Page(1240, 1754 if template != "receipt" else 1500)
        y = 70
        if pno == 1:
            if template == "receipt":
                pg.row(y, [(pg.w // 2, vendor, "ma")], 40, True); y += 60
                pg.row(y, [(pg.w // 2, addr, "ma")], 24); y += 36
                pg.row(y, [(pg.w // 2, f"Tel {phone}", "ma")], 24); y += 50
                pg.row(y, [(80, f"Receipt No: {inv_no}", "la")], 26); y += 40
                pg.row(y, [(80, f"Date: {d0.strftime('%d/%m/%Y')}", "la")], 26); y += 60
            else:
                pg.row(y, [(80, vendor, "la")], 42, True); y += 62
                pg.row(y, [(80, addr, "la")], 24); y += 34
                pg.row(y, [(80, f"Tel: {phone}", "la")], 24); y += 34
                if template == "nepal":
                    pg.row(y, [(80, f"PAN No: {pan}", "la")], 24); y += 34
                y += 20
                pg.row(y, [(80, "INVOICE" if template != "nepal" else "TAX INVOICE", "la")], 34, True); y += 54
                pg.row(y, [(80, f"Invoice No: {inv_no}", "la")], 26); y += 40
                if template == "nepal":
                    pg.row(y, [(80, f"Miti: {bs_text}", "la")], 26); y += 40
                else:
                    pg.row(y, [(80, f"Date: {d0.strftime('%d/%m/%Y')}", "la")], 26); y += 40
                    pg.row(y, [(80, f"Due Date: {due.strftime('%d/%m/%Y')}", "la")], 26); y += 40
                if template == "standard":
                    pg.row(y, [(80, "Bill To:", "la")], 26, True); y += 38
                    pg.row(y, [(80, customer, "la")], 26); y += 50
        else:
            pg.row(y, [(80, f"{vendor} - Invoice {inv_no} (continued)", "la")], 24); y += 60
        # table
        if template == "receipt":
            for it in chunk:
                pg.row(y, [(80, it["description"], "la"), (700, it["quantity"], "ra"), (900, money(Decimal(it["unit_price"])), "ra"),
                           (1130, money(Decimal(it["amount"])), "ra")], 26); y += 40
        else:
            pg.row(y, [(80, "Description", "la"), (640, "Qty", "la"), (780, "Rate", "la"), (1000, "Amount", "la")], 26, True); y += 46
            pg.d.line([(80, y - 8), (1160, y - 8)], fill="black", width=2)
            for it in chunk:
                pg.row(y, [(80, it["description"], "la"), (690, it["quantity"], "ra"), (900, money(Decimal(it["unit_price"])), "ra"),
                           (1140, money(Decimal(it["amount"])), "ra")], 26); y += 42
        if pno == len(chunks):
            y += 30
            pg.d.line([(600, y - 10), (1160, y - 10)], fill="black", width=2)
            def trow(label, val, bold=False):
                nonlocal y
                pg.row(y, [(660, label, "la"), (1140, money(val) if not isinstance(val, str) else val, "ra")], 26, bold); y += 42
            trow("Sub Total", tot["subtotal"])
            if tot["discount"] is not None:
                trow("Discount", tot["discount"]); trow("Taxable Amount", tot["taxable_amount"])
            trow("VAT 13%", tot["tax"])
            trow("Grand Total", tot["total"], True)
        else:
            pg.row(pg.h - 120, [(pg.w // 2, f"Page {pno} of {len(chunks)}", "ma")], 22)
        imgs.append(_degrade(pg.img, degradation, rng))
        all_rows.append(pg.rows)

    blobs = []
    for im in imgs:
        b = io.BytesIO(); im.save(b, "PNG"); blobs.append(b.getvalue())
    return Sample(id=f"syn-{seed}-{idx:04d}", template=template, degradation=degradation, group_key=f"{template}:{vendor}",
                  images=blobs, truth=truth, ocr_text="\n".join("\n".join(r) for r in all_rows),
                  meta={"cur_label": cur_label, "synthetic": True})


def generate(n: int, seed: int = 7, degradations: tuple[str, ...] = DEGRADATIONS, multipage_every: int = 6) -> list[Sample]:
    out = []
    for i in range(n):
        tpl = TEMPLATES[i % len(TEMPLATES)]
        deg = degradations[(i // len(TEMPLATES)) % len(degradations)]
        pages = 2 if (multipage_every and i % multipage_every == multipage_every - 1 and tpl != "receipt") else 1
        out.append(make_sample(i, seed, tpl, deg, pages))
    return out


def write_dataset(samples: list[Sample], directory: Path) -> Path:
    """Directory layout: <id>/page-N.png + <id>/truth.json; manifest.jsonl lists samples. Marked synthetic in the manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "manifest.jsonl").open("w") as mf:
        for s in samples:
            sd = directory / s.id
            sd.mkdir(exist_ok=True)
            pages = []
            for i, b in enumerate(s.images, start=1):
                (sd / f"page-{i}.png").write_bytes(b); pages.append(f"{s.id}/page-{i}.png")
            (sd / "truth.json").write_text(json.dumps({"truth": s.truth, "ocr_text": s.ocr_text}, sort_keys=True, indent=1))
            mf.write(json.dumps({"id": s.id, "pages": pages, "truth": f"{s.id}/truth.json", "template": s.template,
                                 "degradation": s.degradation, "group_key": s.group_key, "synthetic": True}) + "\n")
    return directory / "manifest.jsonl"
