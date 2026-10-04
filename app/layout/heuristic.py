"""Deterministic layout analysis: row grouping, header-keyword table detection, column assignment by x position."""
from __future__ import annotations

import re
from statistics import median

from app.layout.base import LAYOUT_VERSION, Cell, Column, Layout, LayoutAnalyzer, LayoutPage, Row, Table
from app.ocr.base import OCRPage, OCRResult

_COL_KEYWORDS = {
    "description": re.compile(r"^(description|item|items|particulars|product|details|name|goods)$", re.I),
    "quantity": re.compile(r"^(qty|quantity|nos|no\.?|units?)$", re.I),
    "unit": re.compile(r"^(uom|unit)$", re.I),
    "unit_price": re.compile(r"^(rate|price|unit\s*price|mrp|u/price)$", re.I),
    "discount": re.compile(r"^(disc|discount|disc\.)$", re.I),
    "tax": re.compile(r"^(vat|tax|gst)$", re.I),
    "amount": re.compile(r"^(amount|total|value|line\s*total|amt)$", re.I),
}
TOTALS_ROW = re.compile(r"^\s*(?:[\d/|]+\s+)?(sub\s*-?\s*total|[A-Za-z]{3,}\s+total\b|total|grand\s*total|net\s*(amount|payable)|vat|tax|discount|taxable|"
                        r"service\s*charge|amount\s*(due|paid|payable)|balance|paid|round)", re.I)
FOOTER_ROW = re.compile(r"^\s*(page\s+\d+|\d+\s*/\s*\d+$|continued|\(continued\))", re.I)
NUM = re.compile(r"^-?[\d][\d,]*(?:\.\d{1,2})?$")
# till-receipt item row: "<description> <qty> <rate> <amount>"
_ITEM_ROW = re.compile(r"^\W*(?P<d>[A-Za-z][^\d]*?[A-Za-z.)]\w*(?:\s+[A-Za-z][\w.&/-]*)*)\s+\d+(?:\.\d+)?\s+[\d,]+(?:\.\d{1,2})?\W*\s+[\d,]+(?:\.\d{1,2})?\W*$")


def _cells(line) -> list[Cell]:
    """Word boxes when the engine supplies them, otherwise the line box itself."""
    if line.words:
        return [Cell(w.text, w.bbox, w.confidence) for w in line.words]
    return [Cell(line.text, line.bbox, line.confidence)]


def group_rows(page: OCRPage) -> list[Row]:
    """Cluster boxes that share a baseline into one row. Vertical-overlap test (not exact y) tolerates small skew."""
    cells = [c for ln in page.lines for c in _cells(ln)]
    if not cells:
        return []
    cells.sort(key=lambda c: ((c.bbox[1] + c.bbox[3]) / 2, c.bbox[0]))
    med_h = median(c.bbox[3] - c.bbox[1] for c in cells) or 1
    rows: list[list[Cell]] = []
    cy: list[float] = []
    for c in cells:
        y = (c.bbox[1] + c.bbox[3]) / 2
        if rows and abs(y - cy[-1]) <= 0.55 * max(med_h, c.bbox[3] - c.bbox[1]):
            rows[-1].append(c); cy[-1] = sum((x.bbox[1] + x.bbox[3]) / 2 for x in rows[-1]) / len(rows[-1])
        else:
            rows.append([c]); cy.append(y)
    return [Row(page.number, sorted(r, key=lambda c: c.bbox[0])) for r in rows]


def _header_columns(row: Row, page_width: int) -> list[Column] | None:
    """A header row has >= 2 recognised keywords including a description-like and an amount-like column."""
    found: list[tuple[str, Cell]] = []
    used = set()
    cells = list(row.cells)
    i = 0
    while i < len(cells):
        c = cells[i]
        t = c.text.strip().strip(":")
        hit = False
        for name, rx in _COL_KEYWORDS.items():
            if name not in used and rx.match(t):
                found.append((name, c)); used.add(name); hit = True; break
        if not hit and i + 1 < len(cells):
            # OCR sometimes splits one header word in two ("part" "iculars"): try the joined text
            nxt = cells[i + 1]
            joined = (t + nxt.text.strip().strip(":"))
            for name, rx in _COL_KEYWORDS.items():
                if name not in used and rx.match(joined):
                    found.append((name, Cell(joined, (c.bbox[0], min(c.bbox[1], nxt.bbox[1]), nxt.bbox[2], max(c.bbox[3], nxt.bbox[3])),
                                             (c.confidence + nxt.confidence) / 2)))
                    used.add(name); hit = True; i += 1; break
        i += 1
    names = {n for n, _ in found}
    if "description" not in names and {"amount", "quantity", "unit_price"} <= names and cells:
        # Till receipts: the first column is the item name even when its header is garbled by OCR.
        lead = cells[0]
        if lead.bbox[2] <= min(c.bbox[0] for _, c in found):
            found.append(("description", lead)); names.add("description")
    if len(found) < 3 or not ({"description"} & names) or not ({"amount", "unit_price"} & names):
        return None
    found.sort(key=lambda nc: nc[1].bbox[0])
    cols: list[Column] = []
    centers = [(c.bbox[0] + c.bbox[2]) / 2 for _, c in found]
    for i, (name, c) in enumerate(found):
        left = 0 if i == 0 else int((centers[i - 1] + centers[i]) / 2)
        right = page_width if i == len(found) - 1 else int((centers[i] + centers[i + 1]) / 2)
        cols.append(Column(name, left, right))
    return cols


def _assign(row: Row, cols: list[Column]) -> dict[str, list[Cell]]:
    out: dict[str, list[Cell]] = {}
    for c in row.cells:
        mid = (c.bbox[0] + c.bbox[2]) / 2
        # right-aligned numbers sit under the right end of their header, so judge numeric cells by their right edge
        probe = c.bbox[2] if NUM.match(c.text.replace(" ", "")) else mid
        col = next((k for k in cols if k.x0 <= probe < k.x1), cols[-1] if probe >= cols[-1].x1 else cols[0])
        if col.name in ("quantity", "unit_price") and re.search(r"[A-Za-z]{2}", c.text) and not NUM.match(c.text.replace(" ", "")):
            col = next((k for k in cols if k.name == "description"), col)   # a long item name runs into the next column; words never belong to a numeric column
        out.setdefault(col.name, []).append(c)
    return out


class HeuristicLayoutAnalyzer(LayoutAnalyzer):
    name, version = "heuristic-layout", LAYOUT_VERSION

    def analyze(self, ocr: OCRResult) -> Layout:
        pages: list[LayoutPage] = []
        open_table: Table | None = None       # table from a previous page that has no footer yet
        for p in ocr.pages:
            rows = group_rows(p) if p.status == "ok" else []
            lp = LayoutPage(p.number, p.width, p.height, rows)
            i = 0
            cur: Table | None = None
            if open_table is not None and rows:
                # Continuation page: same columns, body starts after any repeated header or running title.
                cur = Table(p.number, open_table.header, open_table.columns, continued_from_previous=True)
                lp.tables.append(cur)
            while i < len(rows):
                r = rows[i]
                cols = _header_columns(r, p.width)
                if cols is not None:
                    if cur is not None and cur.continued_from_previous and not cur.rows:
                        lp.tables.remove(cur)       # the page repeats the header: use the repeated one
                    cur = Table(p.number, r, cols)
                    lp.tables.append(cur)
                    i += 1
                    continue
                if cur is not None and cur.closed and cur.rows and _ITEM_ROW.match(r.text) and not TOTALS_ROW.match(r.text):
                    cur.closed = False          # another section of the same bill ("Food" subtotal, then "Liquor" items): reopen
                    cur.reopened = True
                    lp.table_end = None
                elif cur is not None and cur.reopened and not cur.closed and not _ITEM_ROW.match(r.text):
                    cur.closed = True           # a reopened section accepts item rows only: anything else (noise, footer text) ends it
                    cur.reopened = False
                if cur is not None and not cur.closed:
                    if FOOTER_ROW.match(r.text):
                        pass                      # page furniture, not a body row
                    elif TOTALS_ROW.match(r.cells[0].text if r.cells else "") or self._is_totals_row(r):
                        cur.closed = True
                        lp.totals_start = lp.totals_start if lp.totals_start is not None else i
                        lp.table_end = i
                    else:
                        cur.rows.append(r)
                i += 1
            if lp.totals_start is None:
                # no table: totals region = trailing rows that look like label + amount
                for j in range(len(rows) - 1, -1, -1):
                    if TOTALS_ROW.match(rows[j].text):
                        lp.totals_start = j
                    elif sum(ch.isalnum() for ch in rows[j].text) < 2:
                        continue                  # stray separator/noise row (a printed dashed line) does not end the totals block
                    elif lp.totals_start is not None:
                        break
            open_table = cur if (cur is not None and not cur.closed) else None
            pages.append(lp)
        return Layout(pages)

    @staticmethod
    def _is_totals_row(r: Row) -> bool:
        """Label in the right half followed by an amount, with nothing in the left description area: a totals line."""
        if len(r.cells) < 2:
            return False
        first = r.cells[0].text
        return bool(TOTALS_ROW.match(first)) or bool(TOTALS_ROW.match(r.text))


def table_items(table: Table) -> list[dict]:
    """Table body rows -> dicts of column name -> (text, cells). Continuation rows (text only) extend the previous item."""
    items: list[dict] = []
    for r in table.rows:
        cells = _assign(r, table.columns)
        has_num = any(NUM.match(c.text.replace(" ", "")) for k, cs in cells.items() if k != "description" for c in cs)
        desc_cells = cells.get("description", [])
        if not has_num and desc_cells and items:
            items[-1]["description"] = (items[-1]["description"] + " " + " ".join(c.text for c in desc_cells)).strip()
            items[-1]["rows"].append(r)
            continue
        if not has_num:
            continue
        item = {"rows": [r], "page": r.page, "bbox": r.bbox, "text": r.text, "confidence": r.confidence}
        for name, cs in cells.items():
            item[name] = " ".join(c.text for c in cs).strip()
        item.setdefault("description", "")
        items.append(item)
    return items
