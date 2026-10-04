import io
import json
import zlib

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject, TextStringObject

from app.core.errors import ValidationFailed
from app.security import sandbox
from app.security.content_checks import ContentRejected, Limits, check_pdf
from app.security.file_validation import validate_upload
from tests.fakes import pdf_bytes, png_bytes

LIM = Limits(max_pixels=1_000_000, max_pdf_pages=5, max_pdf_objects=50_000, max_decoded_bytes=1_000_000)


def writer_pdf(mutate) -> bytes:
    w = PdfWriter()
    for p in PdfReader(io.BytesIO(pdf_bytes())).pages:
        w.add_page(p)
    mutate(w)
    b = io.BytesIO(); w.write(b); return b.getvalue()


def rejected(data, lim=LIM) -> str:
    with pytest.raises(ContentRejected) as e:
        check_pdf(data, lim)
    return e.value.code


def test_valid_pdf_passes():
    assert check_pdf(pdf_bytes(2), LIM) == 2


def test_hex_escaped_javascript_name_evades_raw_scan_but_not_structural_check():
    data = writer_pdf(lambda w: w.add_js("app.alert(1)")).replace(b"/JavaScript", b"/Java#53cript")
    assert b"/JavaScript" not in data                                  # raw byte scan alone would miss this
    assert rejected(data) == "pdf_active_content"


def test_hex_escaped_openaction_rejected():
    def m(w):
        w._root_object[NameObject("/OpenAction")] = DictionaryObject({NameObject("/S"): NameObject("/URI")})
    data = writer_pdf(m).replace(b"/OpenAction", b"/Open#41ction")
    assert rejected(data) == "pdf_active_content"


def test_launch_action_on_annotation_rejected():
    def m(w):
        act = DictionaryObject({NameObject("/S"): NameObject("/Launch"), NameObject("/F"): TextStringObject("cmd.exe")})
        annot = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
                                  NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4), NameObject("/A"): act})
        w.pages[0][NameObject("/Annots")] = ArrayObject([w._add_object(annot)])
    data = writer_pdf(m).replace(b"/Launch", b"/Laun#63h")
    assert rejected(data) == "pdf_active_content"


def test_file_attachment_annotation_rejected():
    def m(w):
        annot = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/FileAttachment"),
                                  NameObject("/Rect"): ArrayObject([NumberObject(0)] * 4)})
        w.pages[0][NameObject("/Annots")] = ArrayObject([w._add_object(annot)])
    assert rejected(writer_pdf(m)) == "pdf_active_content"


def test_flate_decompression_bomb_in_content_stream_rejected():
    def m(w):
        bomb = StreamObject()
        bomb._data = zlib.compress(b"\x00" * 20_000_000, 9)          # ~20KB on the wire, 20MB decoded
        bomb[NameObject("/Filter")] = NameObject("/FlateDecode")
        w.pages[0][NameObject("/Contents")] = w._add_object(bomb)
    data = writer_pdf(m)
    assert len(data) < 100_000
    assert rejected(data) == "pdf_resource_limit"


def test_oversized_embedded_image_dimensions_rejected():
    def m(w):
        img = StreamObject(); img._data = b"\x00"
        img.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(50_000), NameObject("/Height"): NumberObject(50_000),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})
        res = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): w._add_object(img)})})
        w.pages[0][NameObject("/Resources")] = res
    assert rejected(writer_pdf(m)) == "pdf_resource_limit"


def test_object_count_limit():
    assert rejected(pdf_bytes(), Limits(1_000_000, 5, 2, 1_000_000)) == "pdf_resource_limit"


def test_truncated_pdf_and_garbage_rejected():
    assert rejected(pdf_bytes()[:200]) == "invalid_file"
    assert rejected(b"%PDF-1.7\n" + b"\x00" * 500) == "invalid_file"


# ---------------------------------------------------------------- sandbox
def test_sandbox_child_has_empty_env_and_rlimits():
    out = json.loads(sandbox.run("tests.sandbox_helpers.inspect_env", "{}", b"", sandbox.SandboxLimits(20, 512)))
    assert set(out["env"]) <= {"PATH", "LANG", "SANDBOX_MEM_MB", "SANDBOX_CPU_S", "LC_CTYPE"}
    assert out["as"] == 512 * 1024 * 1024 and out["core"] == 0


def test_sandbox_timeout_kills_child():
    with pytest.raises(sandbox.SandboxError) as e:
        sandbox.run("tests.sandbox_helpers.sleeper", "{}", b"", sandbox.SandboxLimits(1, 512))
    assert e.value.reason == "timeout"


def test_sandbox_secrets_not_inherited(monkeypatch):
    monkeypatch.setenv("OCR_DATABASE_URL", "postgresql://user:SECRETPW@h/db")
    out = sandbox.run("tests.sandbox_helpers.inspect_env", "{}", b"", sandbox.SandboxLimits(20, 512))
    assert b"SECRETPW" not in out and b"OCR_DATABASE_URL" not in out


def test_sandboxed_validation_accepts_good_and_rejects_bad():
    sb = sandbox.SandboxLimits(30, 1024)
    kw = dict(max_bytes=5_000_000, max_pdf_pages=5, max_image_pixels=1_000_000, sandbox_limits=sb)
    assert validate_upload(io.BytesIO(png_bytes()), filename="a.png", **kw).content_type == "image/png"
    assert validate_upload(io.BytesIO(pdf_bytes(2)), filename="a.pdf", **kw).page_count == 2
    big = io.BytesIO(); __import__("PIL.Image", fromlist=["x"]).new("1", (5000, 5000)).save(big, "PNG")
    with pytest.raises(ValidationFailed) as e:
        validate_upload(io.BytesIO(big.getvalue()), filename="b.png", **kw)
    assert e.value.code == "image_too_large"


def test_sandbox_memory_exhaustion_is_contained():
    """A child that tries to allocate 2GB under a 256MB address-space limit dies; the parent just sees SandboxError."""
    with pytest.raises(sandbox.SandboxError) as e:
        sandbox.run("tests.sandbox_helpers.hog", "{}", b"", sandbox.SandboxLimits(30, 256))
    assert e.value.reason in ("crashed", "killed")


def test_sandbox_failure_maps_to_safe_validation_error(monkeypatch):
    def boom(*a, **k): raise sandbox.SandboxError("killed")
    monkeypatch.setattr(sandbox, "run", boom)
    with pytest.raises(ValidationFailed) as e:
        validate_upload(io.BytesIO(png_bytes()), filename="a.png", max_bytes=5_000_000, max_pdf_pages=5,
                        max_image_pixels=1_000_000, sandbox_limits=sandbox.SandboxLimits(5, 256))
    assert e.value.code == "document_too_complex"


def test_in_process_image_check_does_not_leak_pillow_pixel_limit():
    """Regression: check_image used to leave Image.MAX_IMAGE_PIXELS lowered, breaking later PDF page loading."""
    from PIL import Image
    from app.security.content_checks import Limits, check_image
    from tests.fakes import png_bytes
    before = Image.MAX_IMAGE_PIXELS
    check_image(png_bytes(), Limits(max_pixels=1_000_000, max_pdf_pages=5, max_pdf_objects=1000, max_decoded_bytes=1_000_000))
    assert Image.MAX_IMAGE_PIXELS == before
