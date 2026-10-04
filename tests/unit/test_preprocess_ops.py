import pytest
from PIL import Image, ImageDraw

from app.core.config import Settings
from app.ocr.preprocess_ops import apply_ops, estimate_skew_degrees, parse_ops, preprocess_version


def page(w=600, h=400):
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for y in range(40, h - 40, 30):
        d.rectangle([40, y, w - 40, y + 8], fill="black")          # text-like horizontal bars
    return img


def test_parse_ops_and_unknown_rejected():
    assert parse_ops("autocontrast, deskew") == ["autocontrast", "deskew"] and parse_ops("") == [] and parse_ops(None) == []
    with pytest.raises(ValueError):
        parse_ops("autocontrast,rm-rf")


def test_version_unchanged_without_ops_and_distinct_with_ops():
    assert preprocess_version("1", []) == "1"
    assert preprocess_version("1", ["deskew"]) != preprocess_version("1", ["deskew", "otsu"])


def test_ops_are_deterministic_and_keep_size():
    src = page()
    for ops in (["autocontrast"], ["median3"], ["sharpen"], ["otsu"], ["deskew"], ["autocontrast", "median3", "otsu", "deskew"]):
        a, b = apply_ops(src.copy(), ops), apply_ops(src.copy(), ops)
        assert a.tobytes() == b.tobytes() and a.size == src.size


def test_deskew_estimates_and_reduces_a_known_rotation():
    straight = page()
    skewed = straight.rotate(2.0, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    assert abs(estimate_skew_degrees(straight)) < 0.3
    est = estimate_skew_degrees(skewed)
    assert 1.0 <= abs(est) <= 3.0
    assert abs(estimate_skew_degrees(apply_ops(skewed, ["deskew"]))) < abs(est)


def test_deskew_leaves_straight_pages_untouched():
    src = page()
    assert apply_ops(src.copy(), ["deskew"]).tobytes() == src.tobytes()


def test_settings_reject_unknown_ops_and_insane_tolerances():
    kw = dict(api_key_pepper="p" * 32, admin_token="a" * 32, database_url="sqlite://")
    Settings(**kw, preprocess_ops="autocontrast,deskew")
    with pytest.raises(Exception):
        Settings(**kw, preprocess_ops="nonsense")
    with pytest.raises(Exception):
        Settings(**kw, validation_tolerance_rel=0.5)
    with pytest.raises(Exception):
        Settings(**kw, ml_max_tenant_share=0)


def test_deskew_does_not_guess_on_low_contrast_or_blank_pages():
    """Regression from the benchmark: a fixed ink threshold saw no ink on a low-contrast scan, all angles tied, and deskew
    rotated the page by the first candidate angle (-5 degrees), destroying the extraction."""
    faint = Image.new("RGB", (600, 400), (235, 235, 235))
    d = ImageDraw.Draw(faint)
    for y in range(40, 360, 30):
        d.rectangle([40, y, 560, y + 8], fill=(205, 205, 205))
    assert estimate_skew_degrees(faint) == 0.0
    assert estimate_skew_degrees(Image.new("RGB", (600, 400), "white")) == 0.0
    assert apply_ops(faint.copy(), ["deskew"]).tobytes() == faint.tobytes()


def test_deskew_needs_a_real_gain_over_zero_degrees():
    assert estimate_skew_degrees(page(), min_gain=1.05) == 0.0
    assert estimate_skew_degrees(page().rotate(1.5, resample=Image.BICUBIC, fillcolor=(255, 255, 255))) != 0.0


def test_new_receipt_ops_are_known_deterministic_and_bounded():
    from app.ocr.preprocess_ops import KNOWN_OPS, UPSCALE_MAX_SIDE_PX, UPSCALE_TARGET_PX
    assert {"upscale", "flatten", "adaptive"} <= set(KNOWN_OPS)
    small = page(423, 694)
    up = apply_ops(small, ["upscale"])
    assert max(up.size) == UPSCALE_TARGET_PX and abs(up.width / up.height - 423 / 694) < 0.01
    big = page(2400, 1600)
    assert apply_ops(big, ["upscale"]).size == big.size            # already large: unchanged
    tiny = page(100, 100)
    assert max(apply_ops(tiny, ["upscale"]).size) <= min(100 * 4, UPSCALE_MAX_SIDE_PX)   # never more than 4x
    for ops in (["flatten"], ["adaptive"], ["upscale", "adaptive"], ["upscale", "flatten", "adaptive"]):
        a, b = apply_ops(small.copy(), ops), apply_ops(small.copy(), ops)
        assert a.tobytes() == b.tobytes()


def test_adaptive_keeps_dark_text_on_a_shadowed_page():
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    for x in range(400):                                            # a left-to-right shadow gradient (a global threshold fails here)
        d.line([x, 0, x, 300], fill=(255 - x // 3,) * 3)
    d.rectangle([40, 100, 360, 112], fill=(10, 10, 10))
    out = apply_ops(img, ["adaptive"]).convert("L")
    assert out.getpixel((200, 106)) == 0 and out.getpixel((200, 200)) == 255 and out.getpixel((380, 200)) == 255


def test_retry_settings_are_validated():
    with pytest.raises(ValueError):
        Settings(retry_preprocess_ops="adaptive,bogus")


def test_retry_settings_load_from_environment_strings(monkeypatch):
    """Docker passes every value as a string: OCR_RETRY_TESSERACT_PSM="6" must load (a Literal[int] field rejected it)."""
    monkeypatch.setenv("OCR_RETRY_TESSERACT_PSM", "6")
    monkeypatch.setenv("OCR_TESSERACT_PSM", "3")
    monkeypatch.setenv("OCR_RETRY_PREPROCESS_OPS", "upscale,adaptive")
    s = Settings()
    assert (s.retry_tesseract_psm, s.tesseract_psm, s.retry_preprocess_ops) == (6, 3, "upscale,adaptive")
    monkeypatch.setenv("OCR_RETRY_TESSERACT_PSM", "7")
    with pytest.raises(ValueError):
        Settings()
