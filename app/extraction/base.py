"""Document understanding: OCR text+layout -> candidate fields.
Separate from OCR so a layout model (LayoutLM-style) can replace the rules without touching OCR."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from typing import Sequence

from PIL import Image

from app.ocr.base import OCRResult


@dataclass
class RawField:
    value: str | None
    confidence: float  # 0..1 evidence strength (OCR confidence x pattern strength) -- heuristic, not a probability
    page: int | None = None                                   # 1-based source page
    bbox: tuple[int, int, int, int] | None = None             # source line/word box in rendered page pixels
    source_text: str | None = None                            # OCR text the value was read from
    method: str | None = None                                 # e.g. "regex:total", "layout:key_value"
    ocr_confidence: float | None = None                       # engine-reported confidence of the source text


@dataclass
class RawItem:
    description: str
    quantity: str | None
    unit_price: str | None
    amount: str | None
    confidence: float
    unit: str | None = None
    discount: str | None = None
    tax: str | None = None
    page: int | None = None
    bbox: tuple[int, int, int, int] | None = None
    source_text: str | None = None


@dataclass
class RawExtraction:
    fields: dict[str, RawField] = field(default_factory=dict)
    items: list[RawItem] = field(default_factory=list)
    hints: dict[str, str] = field(default_factory=dict)  # e.g. {"date": "ambiguous_day_month"}


class Extractor(ABC):
    name: str
    version: str

    @abstractmethod
    def extract(self, ocr: OCRResult, pages: Sequence[Image.Image] | None = None) -> RawExtraction:
        """`pages` (rendered images) is optional input for layout-aware models; rule-based extractors ignore it."""
