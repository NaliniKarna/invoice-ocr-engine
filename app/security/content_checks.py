"""Structural content checks for untrusted PNG/JPEG/PDF bytes.

Deliberately dependency-light (Pillow, pypdf, stdlib) so it can run inside the resource-limited
sandbox child (app/security/validate_child.py) without importing application config.

What this is: a bounded-effort structural parse that rejects malformed files, active content that
the PDF *parser* can see (after resolving hex-escaped names and compressed object streams),
oversized images, and decompression bombs. What it is NOT: a malware scanner. Residual risk is
contained by running this (and rendering) under CPU/memory/time limits.
"""
from __future__ import annotations

import base64
import binascii
import io
import re
import zlib
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject


class ContentRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Limits:
    max_pixels: int
    max_pdf_pages: int
    max_pdf_objects: int
    max_decoded_bytes: int


# Names that make a PDF *active* or carry payloads. Checked structurally on the parsed object graph.
_BAD_ACTIONS = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition", "/Movie",
                "/Sound", "/Hide", "/SetOCGState", "/Trans", "/GoTo3DView"}
_BAD_SUBTYPES = {"/FileAttachment", "/Movie", "/Sound", "/Screen", "/RichMedia", "/3D"}
_RAW_TOKENS = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|OpenAction|RichMedia|XFA|SubmitForm|ImportData)(?![A-Za-z0-9])")
_FLATE = {"/FlateDecode", "/Fl"}
_MAX_WALK = 20_000


def check_image(data: bytes, limits: Limits) -> None:
    # Pillow's pixel cap is process-global. Restore it afterwards so an in-process check (tests, non-sandboxed
    # mode) cannot change the limit seen by unrelated code such as rendered-page loading.
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = limits.max_pixels  # Pillow raises DecompressionBombError at 2x this
    try:
        _check_image(data, limits)
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _check_image(data: bytes, limits: Limits) -> None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            if w <= 0 or h <= 0 or w * h > limits.max_pixels:
                raise ContentRejected("image_too_large", "Image dimensions exceed the allowed limit")
            if getattr(img, "n_frames", 1) > 1:
                raise ContentRejected("invalid_file", "Multi-frame images are not supported")
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            img.load()  # full decode, bounded by MAX_IMAGE_PIXELS (and the sandbox memory limit)
    except ContentRejected:
        raise
    except Image.DecompressionBombError as exc:
        raise ContentRejected("image_too_large", "Image dimensions exceed the allowed limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ContentRejected("invalid_file", "File is not a valid image") from exc


def _name_set(obj) -> set[str]:
    if isinstance(obj, (list, ArrayObject)):
        return {str(x) for x in obj}
    return {str(obj)} if obj is not None else set()


def _resolve(obj):
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def _filter_list(stream: StreamObject) -> list[str]:
    f = _resolve(stream.get("/Filter"))
    if f is None:
        return []
    return [str(_resolve(x)) for x in f] if isinstance(f, (list, ArrayObject)) else [str(f)]


def _check_stream(stream: StreamObject, limits: Limits) -> None:
    """Bound the decoded size of Flate streams without inflating past the cap.
    Decodes ASCII85/ASCIIHex wrappers (common in generated PDFs); stops at any other filter
    (DCT/CCITT/LZW/...), which the sandbox memory limit and the image-dimension check cover."""
    data = getattr(stream, "_data", None)
    if data is None:
        return
    try:
        for flt in _filter_list(stream):
            if flt in ("/ASCII85Decode", "/A85"):
                data = base64.a85decode(data.strip().removesuffix(b"~>"), adobe=False, ignorechars=b" \t\r\n\f\0")
            elif flt in ("/ASCIIHexDecode", "/AHx"):
                hexs = re.sub(rb"[^0-9A-Fa-f]", b"", data.removesuffix(b">"))
                data = binascii.unhexlify(hexs + (b"0" if len(hexs) % 2 else b""))
            elif flt in _FLATE:
                data = zlib.decompressobj().decompress(data, limits.max_decoded_bytes + 1)
                if len(data) > limits.max_decoded_bytes:
                    raise ContentRejected("pdf_resource_limit", "PDF stream expands beyond the allowed size")
            else:
                return
    except (zlib.error, ValueError, binascii.Error):
        raise ContentRejected("invalid_file", "PDF contains a corrupt stream")


def _check_action(action, limits: Limits, budget: list[int], depth: int = 0) -> None:
    action = _resolve(action)
    if not isinstance(action, DictionaryObject) or depth > 8:
        return
    if str(action.get("/S", "")) in _BAD_ACTIONS:
        raise ContentRejected("pdf_active_content", "PDF contains active content")
    if "/JS" in action:
        raise ContentRejected("pdf_active_content", "PDF contains active content")
    if "/Next" in action:
        nxt = _resolve(action["/Next"])
        for a in (nxt if isinstance(nxt, (list, ArrayObject)) else [nxt]):
            _check_action(a, limits, budget, depth + 1)


def _tick(budget: list[int]) -> None:
    budget[0] -= 1
    if budget[0] < 0:
        raise ContentRejected("pdf_resource_limit", "PDF is too complex")


def _check_xobjects(resources, limits: Limits, budget: list[int], depth: int = 0) -> None:
    resources = _resolve(resources)
    if not isinstance(resources, DictionaryObject) or depth > 4:
        return
    xobjs = _resolve(resources.get("/XObject"))
    if not isinstance(xobjs, DictionaryObject):
        return
    for _, ref in list(xobjs.items())[:200]:
        _tick(budget)
        x = _resolve(ref)
        if not isinstance(x, StreamObject):
            continue
        sub = str(x.get("/Subtype", ""))
        if sub == "/Image":
            w, h = int(x.get("/Width", 0) or 0), int(x.get("/Height", 0) or 0)
            if w <= 0 or h <= 0 or w * h > limits.max_pixels:
                raise ContentRejected("pdf_resource_limit", "PDF embeds an oversized image")
        _check_stream(x, limits)
        if sub == "/Form":
            _check_xobjects(x.get("/Resources"), limits, budget, depth + 1)


def check_pdf(data: bytes, limits: Limits) -> int:
    """Returns page count or raises ContentRejected."""
    if _RAW_TOKENS.search(data):  # cheap first pass; the structural pass below is the real check
        raise ContentRejected("pdf_active_content", "PDF contains active or embedded content")
    budget = [_MAX_WALK]
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ContentRejected("pdf_encrypted", "Encrypted PDFs are not supported")
        size = int(reader.trailer.get("/Size", 0) or 0)
        if size > limits.max_pdf_objects:
            raise ContentRejected("pdf_resource_limit", "PDF has too many objects")
        pages = len(reader.pages)
        if pages < 1 or pages > limits.max_pdf_pages:
            raise ContentRejected("pdf_page_limit", f"PDF must have between 1 and {limits.max_pdf_pages} pages")
        root = _resolve(reader.trailer["/Root"])
        if "/OpenAction" in root or "/AA" in root:
            raise ContentRejected("pdf_active_content", "PDF contains active content")
        names = _resolve(root.get("/Names"))
        if isinstance(names, DictionaryObject) and ({"/JavaScript", "/EmbeddedFiles"} & set(names.keys())):
            raise ContentRejected("pdf_active_content", "PDF contains active or embedded content")
        if "/Collection" in root:  # PDF portfolios embed other files
            raise ContentRejected("pdf_active_content", "PDF portfolios are not supported")
        form = _resolve(root.get("/AcroForm"))
        if isinstance(form, DictionaryObject) and "/XFA" in form:
            raise ContentRejected("pdf_active_content", "PDF contains XFA forms")
        for page in reader.pages:
            _tick(budget)
            if "/AA" in page:
                raise ContentRejected("pdf_active_content", "PDF contains active content")
            annots = _resolve(page.get("/Annots"))
            for ref in list(annots or [])[:500]:
                _tick(budget)
                a = _resolve(ref)
                if not isinstance(a, DictionaryObject):
                    continue
                if str(a.get("/Subtype", "")) in _BAD_SUBTYPES or "/AA" in a:
                    raise ContentRejected("pdf_active_content", "PDF contains active content")
                if "/A" in a:
                    _check_action(a["/A"], limits, budget)
            contents = _resolve(page.get("/Contents"))
            for c in (contents if isinstance(contents, (list, ArrayObject)) else [contents]):
                c = _resolve(c)
                if isinstance(c, StreamObject):
                    _check_stream(c, limits)
            _check_xobjects(page.get("/Resources"), limits, budget)
        return pages
    except ContentRejected:
        raise
    except RecursionError as exc:
        raise ContentRejected("pdf_resource_limit", "PDF is too complex") from exc
    except MemoryError as exc:
        raise ContentRejected("pdf_resource_limit", "PDF is too complex") from exc
    except Exception as exc:  # pypdf raises many types on malformed input; all mean "reject"
        raise ContentRejected("invalid_file", "File is not a valid PDF") from exc
