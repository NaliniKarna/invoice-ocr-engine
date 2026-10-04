"""Bytes -> RGB page images. `load_pages` is the in-process implementation (used inside the sandbox child and by
tests); workers call `load_pages_sandboxed`, which runs it in a CPU/memory/time-limited child process."""
from __future__ import annotations

import io
import json
import struct

import pypdfium2 as pdfium
from PIL import Image, ImageOps

from app.ocr.base import DocumentTooComplex, OCRPermanentError
from app.ocr.preprocess_ops import apply_ops, preprocess_version
from app.security import sandbox

PREPROCESS_VERSION = "1"   # bump when decode/render/normalisation behaviour changes (recorded per result, with enabled ops)
MAX_SIDE_PX = 2600


def _normalise(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE_PX:
        img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    return img


def load_pages(data: bytes, content_type: str, *, max_pages: int, max_pixels: int,
               ops: list[str] | None = None) -> list[Image.Image | None]:
    """Returns one entry per page in document order; an entry is None when that page could not be rendered (PDF only)."""
    previous = Image.MAX_IMAGE_PIXELS     # process-global: restore so callers sharing this process are unaffected
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        if content_type == "application/pdf":
            pdf = pdfium.PdfDocument(data)
            try:
                n = len(pdf)
                if n < 1 or n > max_pages:
                    raise OCRPermanentError("PDF page count out of range")
                pages: list[Image.Image | None] = []
                for i in range(n):
                    try:
                        page = pdf[i]
                        w, h = page.get_size()  # points
                        scale = min(2.5, MAX_SIDE_PX / max(w, h))  # ~180 dpi, bounded
                        pages.append(apply_ops(_normalise(page.render(scale=scale).to_pil()), ops or []))
                    except Exception:           # one bad page must not hide the others; caller records it as failed
                        pages.append(None)
                if all(p is None for p in pages):
                    raise OCRPermanentError("Document could not be decoded")
                return pages
            finally:
                pdf.close()
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            return [apply_ops(_normalise(img), ops or [])]
    except OCRPermanentError:
        raise
    except Exception as exc:
        raise OCRPermanentError("Document could not be decoded") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def load_pages_sandboxed(data: bytes, content_type: str, *, max_pages: int, max_pixels: int,
                         limits: sandbox.SandboxLimits, ops: list[str] | None = None) -> list[Image.Image | None]:
    try:
        raw = sandbox.run("app.ocr.render_child", json.dumps({"ct": content_type, "max_pages": max_pages,
                                                                "max_pixels": max_pixels, "ops": ops or []}), data, limits)
    except sandbox.SandboxError as exc:
        raise DocumentTooComplex("Document exceeded rendering limits") from exc
    pages: list[Image.Image | None] = []
    pos = 0
    if raw[:1] == b"E":  # child-reported failure
        raise OCRPermanentError("Document could not be decoded")
    while pos + 4 <= len(raw):
        (n,) = struct.unpack(">I", raw[pos:pos + 4]); pos += 4
        pages.append(Image.open(io.BytesIO(raw[pos:pos + n])).convert("RGB") if n else None); pos += n    # n == 0: page failed
    if not pages or all(p is None for p in pages):
        raise OCRPermanentError("Document could not be decoded")
    return pages
