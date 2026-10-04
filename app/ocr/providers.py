from __future__ import annotations

from typing import Sequence

from PIL import Image

from app.core.config import Settings
from app.ocr.base import OCRLine, OCRWord, OCRNotConfigured, OCRPage, OCRPermanentError, OCRProvider, OCRResult, OCRTransientError


class UnconfiguredProvider(OCRProvider):
    """Default. Fails fast and loudly instead of returning fake data."""
    name, version = "unconfigured", "0"

    def recognize(self, pages: Sequence[Image.Image]) -> OCRResult:
        raise OCRNotConfigured("No OCR provider configured (set OCR_OCR_PROVIDER)")


class TesseractProvider(OCRProvider):
    """Local Tesseract via pytesseract (needs the `tesseract` binary). Reference integration only:
    not validated on real invoices and not a production-quality model for receipts."""
    name = "tesseract"

    def __init__(self, languages: str = "eng", *, auto_orient: bool = False, orient_min_conf: float = 2.0, psm: int = 3):
        self.auto_orient, self.orient_min_conf = auto_orient, orient_min_conf
        self.psm = psm            # Tesseract page segmentation mode; 3 = automatic (engine default), 6 = one uniform block (receipts)
        try:
            import pytesseract
        except ImportError as exc:
            raise OCRPermanentError("pytesseract not installed") from exc
        self._t = pytesseract
        self.languages = languages
        try:
            self.version = str(pytesseract.get_tesseract_version())
        except Exception as exc:
            raise OCRPermanentError("tesseract binary not available") from exc

    def recognize_page(self, img: Image.Image, number: int) -> OCRPage:
        rotation = 0
        if self.auto_orient:
            img, rotation = self._orient(img)
        try:
            d = self._t.image_to_data(img, lang=self.languages, config=f"--psm {self.psm}", output_type=self._t.Output.DICT, timeout=60)
        except RuntimeError as exc:  # timeout
            raise OCRTransientError("OCR timeout") from exc
        except Exception as exc:
            raise OCRPermanentError("OCR engine error") from exc
        lines: dict[tuple, dict] = {}
        for i, txt in enumerate(d["text"]):
            conf = float(d["conf"][i])
            if not txt.strip() or conf < 0:
                continue
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            x0, y0, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            ln = lines.setdefault(key, {"w": [], "c": [], "words": [], "x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h})
            ln["w"].append(txt); ln["c"].append(conf / 100.0)
            ln["words"].append(OCRWord(txt, conf / 100.0, (x0, y0, x0 + w, y0 + h)))
            ln["x0"], ln["y0"] = min(ln["x0"], x0), min(ln["y0"], y0)
            ln["x1"], ln["y1"] = max(ln["x1"], x0 + w), max(ln["y1"], y0 + h)
        return OCRPage(number, img.width, img.height, [
            OCRLine(" ".join(v["w"]), sum(v["c"]) / len(v["c"]), (v["x0"], v["y0"], v["x1"], v["y1"]), v["words"])
            for v in lines.values()], rotation=rotation)

    def _orient(self, img: Image.Image) -> tuple[Image.Image, int]:
        """Rotate by 90/180/270 when Tesseract's orientation detector is confident. Skipped silently when OSD fails."""
        try:
            osd = self._t.image_to_osd(img, output_type=self._t.Output.DICT, timeout=30)
        except Exception:
            return img, 0
        rot, conf = int(osd.get("rotate", 0)), float(osd.get("orientation_conf", 0))
        if rot in (90, 180, 270) and conf >= self.orient_min_conf:
            return img.rotate(-rot, expand=True), rot      # `rotate` from OSD is the clockwise correction needed
        return img, 0


class RapidOCRProvider(OCRProvider):
    """PP-OCR detection+recognition models run through ONNX Runtime (package `rapidocr-onnxruntime`, models ship inside
    the wheel: no runtime download). Optional dependency; produces line/segment boxes, no word boxes."""
    name = "rapidocr-onnx"

    def __init__(self) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            import importlib.metadata as md
        except ImportError as exc:
            raise OCRPermanentError("rapidocr-onnxruntime not installed") from exc
        self._engine = RapidOCR()
        self.version = md.version("rapidocr-onnxruntime")

    def recognize_page(self, img: Image.Image, number: int) -> OCRPage:
        import numpy as np
        try:
            result, _ = self._engine(np.asarray(img.convert("RGB"))[:, :, ::-1])   # RapidOCR expects BGR
        except Exception as exc:
            raise OCRPermanentError("OCR engine error") from exc
        lines: list[OCRLine] = []
        for box, text, score in (result or []):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            if str(text).strip():
                lines.append(OCRLine(str(text).strip(), float(score), (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))))
        lines.sort(key=lambda l: (l.bbox[1], l.bbox[0]))
        return OCRPage(number, img.width, img.height, lines)


def build_ocr_provider(settings: Settings) -> OCRProvider:
    if settings.ocr_provider == "tesseract":
        return TesseractProvider(settings.ocr_languages, auto_orient=settings.ocr_auto_orient, psm=settings.tesseract_psm)
    if settings.ocr_provider == "rapidocr":
        return RapidOCRProvider()
    return UnconfiguredProvider()


def parse_retry_passes(spec: str, default_psm: int = 6) -> list[tuple[list[str], int]]:
    """"upscale@3;upscale,adaptive@6" -> [(["upscale"], 3), (["upscale", "adaptive"], 6)]. Raises ValueError on a bad spec."""
    from app.ocr.preprocess_ops import parse_ops
    out: list[tuple[list[str], int]] = []
    for part in (x.strip() for x in spec.split(";")):
        if not part:
            continue
        ops_text, _, psm_text = part.partition("@")
        psm = int(psm_text) if psm_text.strip() else default_psm
        if psm not in (3, 4, 6, 11):
            raise ValueError("Tesseract page segmentation mode must be one of 3, 4, 6, 11")
        ops = parse_ops(ops_text)
        if not ops:
            raise ValueError("a retry pass needs at least one preprocessing operation")
        out.append((ops, psm))
    return out


def retry_variants(settings: Settings) -> list[tuple[list[str], int]]:
    """(ops, psm) of every retry pass: `retry_passes` when set, otherwise the single retry_preprocess_ops/retry_tesseract_psm pair."""
    from app.ocr.preprocess_ops import parse_ops
    if settings.retry_passes.strip():
        return parse_retry_passes(settings.retry_passes, settings.retry_tesseract_psm)
    ops = parse_ops(settings.retry_preprocess_ops)
    return [(ops, settings.retry_tesseract_psm)] if ops else []


def build_retry_ocr_provider(settings: Settings) -> OCRProvider | None:
    """Provider for the second pass, or None when no retry is configured. Tesseract gets its own page-segmentation mode;
    other engines have no such setting and reuse the default configuration with the retry preprocessing only."""
    variants = retry_variants(settings)
    if not variants:
        return None
    if settings.ocr_provider == "tesseract":
        return TesseractProvider(settings.ocr_languages, auto_orient=settings.ocr_auto_orient, psm=variants[0][1])
    if settings.ocr_provider == "rapidocr":
        return RapidOCRProvider()
    return None


def build_retry_provider_for(settings: Settings, psm: int) -> OCRProvider | None:
    if settings.ocr_provider == "tesseract":
        return TesseractProvider(settings.ocr_languages, auto_orient=settings.ocr_auto_orient, psm=psm)
    if settings.ocr_provider == "rapidocr":
        return RapidOCRProvider()
    return None
