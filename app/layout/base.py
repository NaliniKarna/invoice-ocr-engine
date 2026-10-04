"""Layout abstraction: OCR text boxes -> rows, regions and tables.

Engines return boxes; invoices are organised in rows (a table row may arrive as several separate boxes) and regions
(header, line-item table, totals). A layout-aware extractor needs that structure, and a future model-based layout
analyzer (for example a table-structure network) can replace `HeuristicLayoutAnalyzer` behind the same interface
without touching OCR, the API, or the persistence layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.ocr.base import OCRResult

LAYOUT_VERSION = "1"
Box = tuple[int, int, int, int]


@dataclass
class Cell:
    text: str
    bbox: Box
    confidence: float


@dataclass
class Row:
    page: int
    cells: list[Cell]                     # left to right

    @property
    def text(self) -> str:
        return " ".join(c.text for c in self.cells)

    @property
    def bbox(self) -> Box:
        return (min(c.bbox[0] for c in self.cells), min(c.bbox[1] for c in self.cells),
                max(c.bbox[2] for c in self.cells), max(c.bbox[3] for c in self.cells))

    @property
    def confidence(self) -> float:
        return sum(c.confidence for c in self.cells) / len(self.cells)

    @property
    def height(self) -> int:
        b = self.bbox
        return b[3] - b[1]


@dataclass
class Column:
    name: str          # description | quantity | unit | unit_price | discount | tax | amount
    x0: int
    x1: int


@dataclass
class Table:
    page: int
    header: Row
    columns: list[Column]
    rows: list[Row] = field(default_factory=list)       # body rows only (footer/totals excluded)
    closed: bool = False                                # a footer/totals row was seen; False = may continue on next page
    continued_from_previous: bool = False
    reopened: bool = False                              # reopened after a section total (multi-section receipts)


@dataclass
class LayoutPage:
    number: int
    width: int
    height: int
    rows: list[Row]
    tables: list[Table] = field(default_factory=list)
    totals_start: int | None = None     # index into rows where the totals region begins
    table_end: int | None = None


@dataclass
class Layout:
    pages: list[LayoutPage]
    version: str = LAYOUT_VERSION

    @property
    def rows(self) -> list[Row]:
        return [r for p in self.pages for r in p.rows]

    @property
    def tables(self) -> list[Table]:
        return [t for p in self.pages for t in p.tables]


class LayoutAnalyzer(ABC):
    name: str
    version: str

    @abstractmethod
    def analyze(self, ocr: OCRResult) -> Layout: ...
