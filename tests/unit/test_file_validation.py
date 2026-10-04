import io
import zlib

import pytest
from PIL import Image

from app.core.errors import PayloadTooLarge, UnsupportedMedia, ValidationFailed
from app.security.file_validation import sanitize_filename, validate_upload
from tests.fakes import jpeg_bytes, pdf_bytes, png_bytes

KW = dict(max_bytes=1_000_000, max_pdf_pages=3, max_image_pixels=1_000_000)


def v(data, name="x.png", **over):
    return validate_upload(io.BytesIO(data), filename=name, **{**KW, **over})


def test_valid_png_jpeg_pdf():
    assert v(png_bytes()).content_type == "image/png"
    assert v(jpeg_bytes()).extension == "jpg"
    assert v(pdf_bytes(2)).page_count == 2


def test_extension_and_content_type_are_ignored():
    assert v(png_bytes(), name="evil.pdf").content_type == "image/png"


def test_rejects_executable_renamed_as_png():
    with pytest.raises(UnsupportedMedia):
        v(b"MZ\x90\x00" + b"\x00" * 100, name="a.png")


def test_rejects_empty_and_oversize():
    with pytest.raises(ValidationFailed):
        v(b"")
    with pytest.raises(PayloadTooLarge):
        v(png_bytes(), max_bytes=10)


def test_rejects_truncated_image():
    with pytest.raises(ValidationFailed):
        v(png_bytes()[:40])


def test_decompression_bomb_rejected_by_pixel_limit():
    big = io.BytesIO(); Image.new("1", (5000, 5000)).save(big, "PNG")  # tiny on disk, 25M px
    assert len(big.getvalue()) < 100_000
    with pytest.raises(ValidationFailed):
        v(big.getvalue())


def test_pdf_page_limit():
    with pytest.raises(ValidationFailed) as e:
        v(pdf_bytes(4))
    assert e.value.code == "pdf_page_limit"


def test_pdf_with_javascript_rejected():
    evil = pdf_bytes().replace(b"%%EOF", b"") + b"\n1 0 obj << /S /JavaScript /JS (app.alert(1)) >> endobj\n%%EOF"
    with pytest.raises(ValidationFailed) as e:
        v(evil)
    assert e.value.code == "pdf_active_content"


def test_garbage_pdf_rejected():
    with pytest.raises(ValidationFailed):
        v(b"%PDF-1.4\nnot really a pdf")


def test_encrypted_pdf_rejected():
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter(); [w.add_page(p) for p in PdfReader(io.BytesIO(pdf_bytes())).pages]
    w.encrypt("pw"); b = io.BytesIO(); w.write(b)
    with pytest.raises(ValidationFailed) as e:
        v(b.getvalue())
    assert e.value.code == "pdf_encrypted"


@pytest.mark.parametrize("raw,expected", [("../../etc/passwd", "passwd"), ("C:\\x\\inv.png", "inv.png"),
                                          ("a\x00b<script>.png", "a_b_script_.png"), (None, None), ("...", None)])
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected
