"""Optional image preprocessing operations. Deterministic (no randomness), bounded (images are already capped at
MAX_SIDE_PX by the renderer; every operation is O(pixels) or works on a <=600px proxy) and versioned.

No operation is enabled by default in the code (the Docker compose file enables `upscale,flatten`). Whether an operation helps depends on the documents and the OCR engine, so each
one must be justified by the benchmark (`python -m app.ml.cli benchmark --preprocess ...`) before it is turned on
through OCR_PREPROCESS_OPS. The active list is recorded with every result in `preprocessing_version`.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter, ImageOps

OPS_IMPL_VERSION = "1"   # existing ops unchanged; new ops are identified by name in the recorded list
KNOWN_OPS = ("autocontrast", "median3", "sharpen", "otsu", "deskew", "upscale", "flatten", "adaptive")

UPSCALE_TARGET_PX = 2000     # long edge the `upscale` op aims for
UPSCALE_MAX_FACTOR = 4.0     # never enlarge more than this (bounded memory and OCR time)
UPSCALE_MAX_SIDE_PX = 2600   # same hard cap as the renderer (MAX_SIDE_PX)


def _autocontrast(img: Image.Image) -> Image.Image:
    return ImageOps.autocontrast(img.convert("L"), cutoff=1).convert("RGB")


def _median3(img: Image.Image) -> Image.Image:
    return img.filter(ImageFilter.MedianFilter(3))


def _sharpen(img: Image.Image) -> Image.Image:
    return img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))


def _otsu(img: Image.Image) -> Image.Image:
    g = np.asarray(img.convert("L"))
    hist = np.bincount(g.ravel(), minlength=256).astype(np.float64)
    total = g.size
    sum_all = np.dot(np.arange(256), hist)
    w_b = np.cumsum(hist)
    s_b = np.cumsum(hist * np.arange(256))
    w_f = total - w_b
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (w_b * w_f) * ((s_b / w_b) - ((sum_all - s_b) / w_f)) ** 2
    t = int(np.nanargmax(between)) if np.isfinite(between).any() else 128
    return Image.fromarray(((g > t) * 255).astype(np.uint8)).convert("RGB")


def _otsu_threshold(g: "np.ndarray") -> int:
    hist = np.bincount(g.ravel(), minlength=256).astype(np.float64)
    total = g.size
    w_b = np.cumsum(hist)
    s_b = np.cumsum(hist * np.arange(256))
    w_f = total - w_b
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (w_b * w_f) * ((s_b / w_b) - ((s_b[-1] - s_b) / w_f)) ** 2
    return int(np.nanargmax(between)) if np.isfinite(between).any() else 128


def estimate_skew_degrees(img: Image.Image, max_angle: float = 5.0, step: float = 0.25, min_gain: float = 1.05) -> float:
    """Projection-profile skew estimate on a small proxy: the angle that maximises the variance of row sums.

    Benchmark findings that shaped this function: (1) a fixed ink threshold found no ink on low-contrast scans, every angle
    tied, and the first angle (-5 degrees) won, which destroyed the page; the threshold is now Otsu on the page itself and a
    page with almost no ink returns 0. (2) straight pages scored a small spurious angle; an angle is returned only if it
    improves the row-profile variance over 0 degrees by `min_gain` (5%)."""
    g = img.convert("L")
    scale = 600 / max(g.size)
    if scale < 1:
        g = g.resize((max(1, int(g.width * scale)), max(1, int(g.height * scale))), Image.BILINEAR)
    arr = np.asarray(g)
    if int(arr.max()) - int(arr.min()) < 40:
        return 0.0                                   # no usable contrast: do not guess
    ink_mask = arr < _otsu_threshold(arr)
    if ink_mask.mean() < 0.001 or ink_mask.mean() > 0.5:
        return 0.0                                   # blank page, or inverted / noise-dominated
    ink = Image.fromarray((ink_mask * 255).astype(np.uint8))

    def score(a: float) -> float:
        rot = ink.rotate(a, resample=Image.BILINEAR, fillcolor=0) if a else ink
        return float(np.var((np.asarray(rot) > 0).sum(axis=1)))

    base = score(0.0)
    best_angle, best_score = 0.0, base
    for k in range(int(-max_angle / step), int(max_angle / step) + 1):
        a = k * step
        sc = score(a)
        if sc > best_score:
            best_angle, best_score = a, sc
    return best_angle if best_score >= base * min_gain else 0.0


def _deskew(img: Image.Image) -> Image.Image:
    a = estimate_skew_degrees(img)
    if abs(a) < 0.3:
        return img
    return img.rotate(a, resample=Image.BICUBIC, fillcolor=(255, 255, 255), expand=False)


def _upscale(img: Image.Image) -> Image.Image:
    """Phone photos of receipts are often ~400-800 px wide: Tesseract needs roughly 30 px letter height. Enlarge small
    images (bicubic) so the long edge reaches UPSCALE_TARGET_PX. Images that are already large are returned unchanged."""
    long_edge = max(img.size)
    if long_edge >= UPSCALE_TARGET_PX:
        return img
    f = min(UPSCALE_TARGET_PX / long_edge, UPSCALE_MAX_FACTOR, UPSCALE_MAX_SIDE_PX / long_edge)
    if f <= 1.05:
        return img
    return img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))), Image.BICUBIC)


def _flatten(img: Image.Image) -> Image.Image:
    """Remove uneven lighting and colour cast (typical for photos): divide by a heavily blurred copy of the page, then
    stretch contrast. Greyscale output. Deterministic, O(pixels)."""
    g = img.convert("L")
    radius = max(8, max(g.size) // 40)
    bg = np.asarray(g.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    flat = np.clip(np.asarray(g, dtype=np.float32) / np.maximum(bg, 1.0) * 235.0, 0, 255).astype(np.uint8)
    return ImageOps.autocontrast(Image.fromarray(flat), cutoff=1).convert("RGB")


def _box_mean(a: "np.ndarray", k: int) -> "np.ndarray":
    """Mean over a k x k window (edge-replicated) using an integral image."""
    pad = k // 2
    ap = np.pad(a, pad, mode="edge")
    ii = np.pad(ap.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))
    h, w = a.shape
    s = ii[k:k + h, k:k + w] - ii[0:h, k:k + w] - ii[k:k + h, 0:w] + ii[0:h, 0:w]
    return s / float(k * k)


def _adaptive(img: Image.Image, offset: float = 12.0) -> Image.Image:
    """Local-mean threshold (black text on white). Beats a global Otsu threshold on photos with shadows and creases.
    Window ~ 1/25 of the long edge, odd, at least 15 px."""
    g = np.asarray(img.convert("L"), dtype=np.float64)
    k = max(15, max(g.shape) // 25) | 1
    out = (g > _box_mean(g, k) - offset)
    return Image.fromarray((out * 255).astype(np.uint8)).convert("RGB")


_OPS = {"autocontrast": _autocontrast, "median3": _median3, "sharpen": _sharpen, "otsu": _otsu, "deskew": _deskew,
        "upscale": _upscale, "flatten": _flatten, "adaptive": _adaptive}


def parse_ops(spec: str | list[str] | None) -> list[str]:
    ops = [o.strip() for o in (spec.split(",") if isinstance(spec, str) else (spec or [])) if o.strip()]
    bad = [o for o in ops if o not in _OPS]
    if bad:
        raise ValueError(f"unknown preprocess ops: {bad}")
    return ops


def apply_ops(img: Image.Image, ops: list[str]) -> Image.Image:
    for o in ops:
        img = _OPS[o](img)
    return img


def preprocess_version(base: str, ops: list[str]) -> str:
    """Identifier stored with each result. Unchanged from v1 when no operation is enabled."""
    return base if not ops else f"{base}+ops{OPS_IMPL_VERSION}:{','.join(ops)}"
