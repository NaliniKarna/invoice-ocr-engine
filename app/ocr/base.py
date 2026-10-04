"""OCR abstraction. OCR = pixels -> text+geometry. It knows nothing about invoices.
Swap engines (Tesseract, cloud OCR, a custom model) by implementing OCRProvider."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Sequence

from PIL import Image


class OCRError(Exception):
    retryable = False
    code = "ocr_failed"


class OCRTransientError(OCRError):
    retryable = True
    code = "ocr_transient"


class OCRPermanentError(OCRError):
    retryable = False


class DocumentTooComplex(OCRPermanentError):
    code = "document_too_complex"


class OCRNotConfigured(OCRPermanentError):
    code = "ocr_not_configured"


@dataclass
class OCRWord:
    text: str
    confidence: float  # 0..1, as reported by the engine
    bbox: tuple[int, int, int, int]


@dataclass
class OCRLine:
    text: str
    confidence: float  # 0..1
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in page pixels
    words: list[OCRWord] = field(default_factory=list)   # optional word-level boxes (layout analysis)


@dataclass
class OCRPage:
    """One page of OCR output. `status` is "ok" or "failed"; a failed page has no lines and carries a safe error code.
    Page order is the order of the document (1-based `number`); results are never re-ordered or merged across pages."""
    number: int
    width: int
    height: int
    lines: list[OCRLine] = field(default_factory=list)
    status: str = "ok"
    error_code: str | None = None
    rotation: int = 0           # degrees the page was rotated before recognition (orientation correction), 0 if none


@dataclass
class OCRResult:
    provider: str
    model_version: str
    pages: list[OCRPage]

    @property
    def lines(self) -> list[OCRLine]:
        return [ln for pg in self.pages for ln in pg.lines]

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def failed_pages(self) -> list[int]:
        return [pg.number for pg in self.pages if pg.status != "ok"]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OCRResult":
        def line(x: dict) -> OCRLine:
            return OCRLine(x["text"], x["confidence"], tuple(x["bbox"]),  # type: ignore[arg-type]
                           [OCRWord(w["text"], w["confidence"], tuple(w["bbox"])) for w in x.get("words", [])])  # type: ignore[arg-type]
        return cls(d["provider"], d["model_version"], [
            OCRPage(p["number"], p["width"], p["height"], [line(x) for x in p.get("lines", [])],
                    p.get("status", "ok"), p.get("error_code"), p.get("rotation", 0)) for p in d["pages"]])


class OCRProvider(ABC):
    name: str
    version: str

    def recognize_page(self, image: Image.Image, number: int) -> OCRPage:
        """Recognize one page. Engines implement this (preferred) or override `recognize`."""
        raise NotImplementedError

    def recognize(self, pages: Sequence[Image.Image]) -> OCRResult:
        """Page-level error handling shared by all engines:
        * transient error on any page (timeout, engine busy) -> re-raised, the whole job is retried;
        * permanent error on one page -> that page is recorded as failed and the others are still read, so the caller
          can flag the document for review instead of silently dropping or inventing content;
        * every page failed -> the last permanent error is raised."""
        out: list[OCRPage] = []
        last: OCRPermanentError | None = None
        for n, img in enumerate(pages, start=1):
            try:
                out.append(self.recognize_page(img, n))
            except OCRTransientError:
                raise
            except OCRPermanentError as exc:
                last = exc
                out.append(OCRPage(n, img.width, img.height, [], status="failed", error_code=exc.code))
        if out and all(p.status != "ok" for p in out) and last is not None:
            raise last
        return OCRResult(self.name, self.version, out)


class ExtractionError(OCRPermanentError):
    """Extraction produced no usable, schema-valid output (model crash, malformed output). Not retried: the same
    document would fail the same way; the job fails with a safe code instead of returning invented data."""
    code = "extraction_failed"
