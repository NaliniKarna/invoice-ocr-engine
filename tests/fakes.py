"""Test doubles. They live in tests/, never in app/, so nothing fake can be deployed."""
from __future__ import annotations

import io
from typing import Sequence

from PIL import Image

from app.ocr.base import OCRLine, OCRPage, OCRPermanentError, OCRProvider, OCRResult, OCRTransientError

INVOICE_LINES = [
    "Himalayan Traders Pvt Ltd", "Invoice No: INV-2026-0042", "Date: 15/03/2026",
    "Description Qty Rate Amount",
    "Rice 25kg 2 1,500.00 3,000.00",
    "Cooking Oil 1 1,200.00 1,200.00",
    "Sub Total 4,200.00", "VAT 13% 546.00", "Grand Total NPR 4,746.00",
]


class FakeOCR(OCRProvider):
    name, version = "fake-ocr", "1.0"

    def __init__(self, lines=None, conf=0.95):
        self.lines, self.conf = lines or INVOICE_LINES, conf
        self.fail_with: Exception | None = None
        self.calls = 0

    def recognize(self, pages: Sequence[Image.Image]) -> OCRResult:
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        ls = [OCRLine(t, self.conf, (0, i * 20, 100, i * 20 + 18)) for i, t in enumerate(self.lines)]
        return OCRResult(self.name, self.version, [OCRPage(1, 100, 200, ls)])


class InMemoryQueue:
    def __init__(self):
        self.items: list[str] = []
        self.bulk_flags: dict[str, bool] = {}
        self.fail = False
        self.reported_depth: int | None = 0        # interactive queue depth
        self.reported_bulk_depth: int | None = 0

    def enqueue(self, process_id: str, *, bulk: bool = False) -> None:
        if self.fail:
            raise RuntimeError("broker down")
        self.items.append(process_id)
        self.bulk_flags[process_id] = bulk

    def depth(self, *, bulk: bool = False):
        return self.reported_bulk_depth if bulk else self.reported_depth


def png_bytes(size=(120, 160), color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def jpeg_bytes(size=(120, 160)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "JPEG")
    return buf.getvalue()


def unique_png(n: int) -> bytes:
    """Distinct content per n (different file hash)."""
    buf = io.BytesIO()
    img = Image.new("RGB", (64, 64), "white")
    img.putpixel((n % 64, (n // 64) % 64), (n % 255, 10, 10))
    img.save(buf, "PNG")
    return buf.getvalue()


def pdf_bytes(pages=1) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"Page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()
