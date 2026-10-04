"""Geometry-based extractor ("layout-kv"): label -> value pairs, dynamic tables, arithmetic arbitration.

Why it exists: `layout-rules` finds fields with fixed label regexes and fixed table column names. A new vendor with
"Taxes" instead of "Tax", a leading Date column, or "$1,234.56" instead of "1234.56" breaks it. This extractor
does not know vendors or templates. It uses only what any invoice has in common:

1. PHRASES: OCR words on one line are merged into phrases by the size of the gap between them. A column gap is much wider than a
   word gap, so "Unit Price" is one phrase and "Qty" and "Rate" are two.
2. LABEL -> VALUE PAIRS: a label phrase and the value to its right, below it, or after a colon, in the same phrase.
   Labels are scored by `lexicon.py` (a word list with stemming and fuzzy matching, not a rule per vendor). Values are
   type-checked (date, money, identifier) so a label can never grab a value of the wrong kind.
3. TABLES FROM CONTENT: a header row is a row of header-like words followed by numbers. Column roles come from the numbers
   below it: the (quantity, unit price, amount) triple is the one where quantity x price = amount on most rows. Header
   words are only a hint, so unknown column names and extra columns (Date, SKU, HSN, Disc%) do not matter.
4. ARITHMETIC ARBITRATION: ambiguous total labels ("Net total", "Amount payable") are resolved by choosing the
   (subtotal, tax, total) combination that adds up. Several roles per label are kept until then.

It does not train anything and its accuracy on real invoices is unmeasured. See docs/LAYOUT_EXTRACTION.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from itertools import product
from statistics import median
from typing import Sequence

from app.extraction.base import RawExtraction, RawField, RawItem
from app.extraction.layout_rules import LayoutRulesExtractor
from app.extraction.lexicon import (date_strength, header_role, invoice_number_strength, is_component_tax, money_roles,
                                    title_like)
from app.extraction.normalization import normalize_date, normalize_money
from app.layout.heuristic import group_rows
from app.ocr.base import OCRPage, OCRResult

_CUR = r"[$€£₹¥]"
_NUMERIC = re.compile(r"^[-(]?\s*\d[\d.,' ]*\)?$")
_DATE_IN = re.compile(r"(?<![\d/.-])(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}[ -][A-Za-z]{3,9}\.?,?[ -]\d{2,4}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4})(?![\d/])")
_ID_LIKE = re.compile(r"^#?[A-Za-z0-9][A-Za-z0-9\-/_.]{0,24}$")
_STREET_WORD = re.compile(r"\b(road|rd|street|st|avenue|ave|lane|floor|suite|plaza|tower|sector|block|park|calle|strasse|straße|nagar|marg|po box|box)\b", re.I)


class _AddressTest:
    """A street word alone ("Lane", "Park", "Tower") can be part of a business name; it only marks an address together with a digit."""
    def search(self, t: str) -> bool:
        return bool(re.search(r"\d{3,}|@|www\.|\.(com|org|net)\b", t, re.I)) or bool(_STREET_WORD.search(t) and re.search(r"\d", t))


_ADDRESS = _AddressTest()
_LEGAL = re.compile(r"\b(llc|ltd|limited|inc|co|corp|corporation|gmbh|ag|sa|plc|pvt|llp|company|traders|enterprises|studio|services|solutions|works|supply|supplies)\b\.?", re.I)
_CUSTOMER_LABEL = {"to", "billed", "bill", "customer", "client", "cliente", "sold", "buyer", "ship", "shipped", "attn", "deliver", "remit", "payable", "cust"}
_VENDOR_LABEL = {"from", "seller", "supplier", "vendor", "issuer"}


# ------------------------------------------------------------------------------------------------ geometry
@dataclass
class Phrase:
    text: str
    x0: int
    y0: int
    x1: int
    y1: int
    conf: float
    page: int
    row: int = 0

    @property
    def h(self) -> int:
        return max(1, self.y1 - self.y0)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)


def _merge(cells, gap_ratio: float = 0.7) -> list[Phrase]:
    out: list[Phrase] = []
    for c in cells:
        x0, y0, x1, y1 = c.bbox
        if out:
            p = out[-1]
            h = max(p.h, y1 - y0)
            if x0 - p.x1 <= gap_ratio * h:
                n = (len(p.text.split()) or 1)
                p.text += " " + c.text
                p.x1, p.y0, p.y1 = max(p.x1, x1), min(p.y0, y0), max(p.y1, y1)
                p.conf = (p.conf * n + c.confidence) / (n + 1)
                continue
        out.append(Phrase(c.text, x0, y0, x1, y1, c.confidence, 0))
    return out


def build_rows(page: OCRPage) -> list[list[Phrase]]:
    rows = []
    for ri, r in enumerate(group_rows(page)):
        ph = _merge(r.cells)
        for p in ph:
            p.page, p.row = page.number, ri
        rows.append(ph)
    return rows


# ------------------------------------------------------------------------------------------------ value typing
def money_value(text: str) -> Decimal | None:
    t = text.strip()
    t = re.sub(r"^(?:Rs\.?|[^\w\s(\-]{1,2}|[A-Za-z])\s+(?=\d)", "", t)      # a currency sign OCR turned into "&", "=", "%", "Z" ...
    if not t or re.search(r"\d\s*%", t) or re.search(r"[A-Za-z]{4,}", t):
        return None
    stripped = re.sub(rf"^[A-Za-z]{{3}}\s*(?=\d)|{_CUR}|(?<=\d)\s*[A-Za-z]{{3}}$", "", t).strip()
    if not _NUMERIC.match(stripped):
        return None
    if re.search(r"\d[-/]\d", stripped):
        return None
    n = normalize_money(t)
    return n.value if n and n.value is not None else None


def is_money(text: str) -> bool:
    return money_value(text) is not None


def date_value(text: str):
    n = normalize_date(text, day_first=True)
    return n.value if n and n.value else None


def find_date(text: str) -> tuple[int, str] | None:
    for m in _DATE_IN.finditer(text):
        if date_value(m.group(1)) is not None:
            return m.start(1), m.group(1)
    return None


def id_like(tok: str, *, min_len: int = 3) -> bool:
    tok = tok.strip().strip(":,;|")
    if not _ID_LIKE.match(tok) or not re.search(r"\d", tok) or len(tok.lstrip("#")) < min_len:
        return False
    if re.fullmatch(r"[\d.,]+", tok) and re.search(r"[.,]\d{2}$", tok) and is_money(tok):
        return False                              # "1,234.56" is an amount; a plain "0042" can be a till-receipt number
    return find_date(tok) is None


def _alpha_ratio(t: str) -> float:
    return sum(c.isalpha() for c in t) / max(1, len(t.replace(" ", "")))


# ------------------------------------------------------------------------------------------------ label/value pairs
@dataclass
class Pair:
    label: str
    value: str
    lp: Phrase           # phrase carrying the label
    vp: Phrase           # phrase carrying the value
    how: str             # colon | typed | right | below


def _pairs(rows: list[list[Phrase]]) -> list[Pair]:
    out: list[Pair] = []
    flat = [p for r in rows for p in r]
    for ri, row in enumerate(rows):
        for k, p in enumerate(row):
            t = p.text.strip()
            if ":" in t:
                a, b = t.split(":", 1)
                if a.strip() and b.strip() and len(a.split()) <= 5 and not re.search(r"\d{2}$", a.strip()):
                    out.append(Pair(a.strip(), b.strip(), p, p, "colon"))
            toks = t.split()
            for j in range(1, len(toks)):          # "Invoice # BL-66750", "Invoice Date 20/10/2025"
                head, tail = " ".join(toks[:j]), " ".join(toks[j:])
                if _alpha_ratio(head) >= 0.5:
                    out.append(Pair(head.rstrip(":"), tail, p, p, "typed"))
            if k + 1 < len(row):                    # label phrase, value phrase to its right
                out.append(Pair(t.rstrip(":"), row[k + 1].text.strip(), p, row[k + 1], "right"))
            for q in flat:                          # value below the label (label-above-value cards)
                if q.page == p.page and q.cy - p.cy > 0.7 * p.h and q.y0 - p.y1 <= 2.2 * p.h and (min(p.x1, q.x1) - max(p.x0, q.x0)) > 0.4 * min(p.x1 - p.x0, q.x1 - q.x0):
                    out.append(Pair(t.rstrip(":"), q.text.strip(), p, q, "below"))
    return out


def _conf(p: Phrase, k: float) -> float:
    return min(1.0, p.conf * k)


def _rf(value: str, vp: Phrase, lp: Phrase, k: float, method: str) -> RawField:
    b = (min(vp.x0, lp.x0), min(vp.y0, lp.y0), max(vp.x1, lp.x1), max(vp.y1, lp.y1))
    return RawField(value, _conf(vp, k), page=vp.page, bbox=b, source_text=f"{lp.text} | {vp.text}"[:300], method=method,
                    ocr_confidence=round(vp.conf, 3))


# ------------------------------------------------------------------------------------------------ tables
@dataclass
class Col:
    x0: int
    x1: int
    text: str
    hint: str
    hint_w: float
    cells: list[str] = field(default_factory=list)
    role: str = ""


@dataclass
class TableParse:
    page: int
    header_row: int
    end_row: int          # index of the first row after the table body (len(rows) when the table runs to the end)
    cols: list[Col]
    items: list[dict]
    left: int
    right: int


def _num(text: str) -> Decimal | None:
    return money_value(text)


def _find_header(rows: list[list[Phrase]], start: int) -> int | None:
    for i in range(start, len(rows)):
        row = rows[i]
        if len(row) < 2:
            continue
        hints = [header_role(p.text) for p in row]
        named = [h for h in hints if h[0]]
        if len(named) < 2 or not any(r in ("amount", "unit_price", "quantity") for r, _ in named):
            continue
        if any(is_money(p.text) or find_date(p.text) for p in row):
            continue
        if not any(is_money(p.text) or re.fullmatch(r"\d+", p.text.strip()) for r2 in rows[i + 1:i + 3] for p in r2):
            continue
        return i
    return None


def _assign(p: Phrase, cols: list[Col]) -> int:
    best, best_ov = None, 0
    for i, c in enumerate(cols):
        ov = min(p.x1, c.x1) - max(p.x0, c.x0)
        if ov > best_ov:
            best, best_ov = i, ov
    if best is not None:
        return best
    return min(range(len(cols)), key=lambda i: min(abs(p.x0 - cols[i].x1), abs(p.x1 - cols[i].x0)))


def parse_table(rows: list[list[Phrase]], start: int = 0) -> TableParse | None:
    hi = _find_header(rows, start)
    if hi is None:
        return None
    hdr = rows[hi]
    named_idx = [i for i, p in enumerate(hdr) if header_role(p.text)[0]]
    lo, hi_i = named_idx[0], named_idx[-1]
    # a header word the vocabulary does not know ("Project", "Fee") is still a column when the rows below have content under it
    h0 = median(p.h for p in hdr)
    below = rows[hi + 1:hi + 4]

    def has_content(p: Phrase) -> bool:
        hits = sum(1 for r in below if any(min(q.x1, p.x1 + 6 * h0) - max(q.x0, p.x0 - h0) > 0 and abs(q.cy - p.cy) > p.h for q in r))
        return bool(below) and hits >= max(1, len(below) // 2 + (len(below) % 2))

    while lo > 0 and _alpha_ratio(hdr[lo - 1].text) > 0.6 and has_content(hdr[lo - 1]):
        lo -= 1
    while hi_i + 1 < len(hdr) and _alpha_ratio(hdr[hi_i + 1].text) > 0.6 and has_content(hdr[hi_i + 1]):
        hi_i += 1
    cols: list[Col] = []
    for p in hdr[lo:hi_i + 1]:
        role, w = header_role(p.text)
        cols.append(Col(p.x0, p.x1, p.text, role, w))
    h = median(p.h for p in hdr)
    left, right = cols[0].x0 - int(2 * h), cols[-1].x1 + int(2 * h)
    # a data cell can be wider than its header: grow every column to the midpoint with its neighbour
    for i, c in enumerate(cols):
        c.x0 = left if i == 0 else (cols[i - 1].x1 + c.x0) // 2 if cols[i - 1].x1 < c.x0 else c.x0
    for i, c in enumerate(cols[:-1]):
        nxt = cols[i + 1]
        c.x1 = (c.x1 + nxt.x0) // 2 if c.x1 < nxt.x0 else c.x1
    cols[-1].x1 = right

    body: list[list[tuple[int, Phrase]]] = []
    end = len(rows)
    for ri in range(hi + 1, len(rows)):
        inr = [p for p in rows[ri] if p.x1 > left and p.x0 < right]
        if not inr:
            continue
        last_money = max((k for k, p in enumerate(inr) if is_money(p.text)), default=-1)     # side text after the amount (a bank block) is not part of the row
        head = inr[:last_money + 1] if last_money >= 0 else inr
        label = " ".join(p.text for p in head if not is_money(p.text))
        roles = money_roles(label) if label and len(head) <= 2 else {}
        if roles and any(r in roles for r in ("subtotal", "tax", "total", "taxable", "due")) and sum(roles.values()) >= 2.4:
            end = ri
            break
        body.append([(_assign(p, cols), p) for p in inr])
    items: list[dict] = []
    for row in body:
        cells: dict[int, str] = {}
        for ci, p in row:
            cells[ci] = (cells.get(ci, "") + " " + p.text).strip()
        has_num = any(_num(t) is not None for t in cells.values())
        if not has_num:
            if items and len(cells) == 1:                # wrapped description line
                items[-1]["cells"][next(iter(cells))] = (items[-1]["cells"].get(next(iter(cells)), "") + " " + next(iter(cells.values()))).strip()
            continue
        items.append({"cells": cells, "phrases": [p for _, p in row]})
    return TableParse(rows[hi][0].page, hi, end, cols, items, left, right)


def _resolve_columns(t: TableParse) -> dict[str, int]:
    n = len(t.cols)
    for it in t.items:
        for i, txt in it["cells"].items():
            t.cols[i].cells.append(txt)
    rows = max(1, len(t.items))
    numeric = [i for i in range(n) if t.cols[i].cells and sum(_num(c) is not None for c in t.cols[i].cells) >= 0.6 * len(t.cols[i].cells) and len(t.cols[i].cells) >= 0.5 * rows]

    def val(it, i):
        return _num(it["cells"].get(i, "")) if i in it["cells"] else None

    best: tuple[float, tuple[int, ...]] | None = None
    for q, p, a in ((q, p, a) for q in numeric for p in numeric for a in numeric if q < p < a):
        ok = tot = 0
        for it in t.items:
            vq, vp, va = val(it, q), val(it, p), val(it, a)
            if vq is None or vp is None or va is None:
                continue
            tot += 1
            ok += abs(vq * vp - va) <= max(Decimal("0.011"), abs(va) * Decimal("0.002"))
        if tot == 0:
            continue
        hint = (t.cols[q].hint == "quantity") + (t.cols[p].hint == "unit_price") + (t.cols[a].hint == "amount")
        score = ok / tot * 10 + 0.3 * hint + 0.01 * a
        if ok / tot >= 0.6 and (best is None or score > best[0]):
            best = (score, (q, p, a))
    roles: dict[str, int] = {}
    if best:
        roles.update(quantity=best[1][0], unit_price=best[1][1], amount=best[1][2])
    else:
        hinted = [i for i in numeric if t.cols[i].hint == "amount"]
        if hinted or numeric:
            roles["amount"] = hinted[-1] if hinted else numeric[-1]
        for i in numeric:
            if i < roles.get("amount", -1) and t.cols[i].hint in ("unit_price", "quantity") and t.cols[i].hint not in roles:
                roles[t.cols[i].hint] = i
    taken = set(roles.values())
    text_cols = [i for i in range(n) if i not in numeric and i not in taken and t.cols[i].cells]
    if text_cols:
        hinted = [i for i in text_cols if t.cols[i].hint == "description"]
        roles["description"] = hinted[0] if hinted else max(text_cols, key=lambda i: (sum(c.isalpha() for c in " ".join(t.cols[i].cells)) / len(t.cols[i].cells), -i))
    return roles


def table_to_items(t: TableParse) -> list[RawItem]:
    roles = _resolve_columns(t)
    out = []
    for it in t.items:
        g = lambda r: (it["cells"].get(roles[r], "").strip() or None) if r in roles else None   # noqa: E731
        desc = g("description") or ""
        amount = g("amount")
        if amount is None and not desc:
            continue
        ph = it["phrases"]
        bbox = (min(p.x0 for p in ph), min(p.y0 for p in ph), max(p.x1 for p in ph), max(p.y1 for p in ph))
        out.append(RawItem(desc, g("quantity"), g("unit_price"), amount, min(1.0, sum(p.conf for p in ph) / len(ph) * 0.9),
                           page=ph[0].page, bbox=bbox, source_text=" ".join(p.text for p in ph)[:300]))
    return out


# ------------------------------------------------------------------------------------------------ totals
@dataclass
class Cand:
    value: Decimal
    text: str
    label: str
    roles: dict[str, float]
    p: Phrase
    order: float            # position in the document, 0 (top of first page) .. 1 (bottom of last page)
    component: bool = False


def _totals_candidates(rows_by_page: list[list[list[Phrase]]], skip: set[tuple[int, int]], page_h: dict[int, int], n_pages: int) -> list[Cand]:
    cands: list[Cand] = []
    for pi, rows in enumerate(rows_by_page):
        for ri, row in enumerate(rows):
            if (pi, ri) in skip:
                continue
            money = [p for p in row if is_money(p.text)]
            for v in money[-1:]:
                label = " ".join(p.text for p in row if p.x1 <= v.x0 + 1 and not is_money(p.text)).strip()
                lp = next((p for p in row if p.x1 <= v.x0 + 1 and not is_money(p.text)), v)
                if not label and ":" in v.text:
                    label = v.text.split(":", 1)[0]
                if not label:                        # label above the amount (banner cards)
                    above = [p for r2 in rows[max(0, ri - 2):ri] for p in r2 if v.cy - p.cy > 0.7 * v.h and v.y0 - p.y1 <= 2.2 * v.h
                             and min(p.x1, v.x1) - max(p.x0, v.x0) > 0.3 * (v.x1 - v.x0)]
                    if above:
                        lp = above[-1]; label = lp.text
                if len(money) > 1 and not label:
                    continue
                if not label:
                    continue
                if len(money) >= 3:                  # an item row, not a totals row
                    continue
                roles = money_roles(label)
                if not roles:
                    continue
                val = money_value(v.text)
                assert val is not None
                cands.append(Cand(val, v.text, label, roles, v, (pi + v.cy / max(1, page_h.get(pi + 1, 1))) / max(1, n_pages), is_component_tax(label)))
    return cands


def _tol(total: Decimal) -> Decimal:
    return max(Decimal("0.02"), abs(total) * Decimal("0.0005"))


def _fits(sub: Decimal, tax: Decimal, total: Decimal, disc: Decimal, charges: Decimal) -> bool:
    diff = abs(sub - disc + tax + charges - total)
    return diff <= _tol(total) or (total == total.to_integral_value() and diff < Decimal("0.5"))


def reconcile(cands: list[Cand], items_sum: Decimal | None) -> dict[str, tuple[Cand | None, str]]:
    """Choose (subtotal, tax, total, ...) among label candidates; the combination that adds up wins."""
    def pool(role: str, floor: float = 0.0) -> list[tuple[Cand, float]]:
        return [(c, c.roles[role]) for c in cands if c.roles.get(role, 0) > floor]

    def best(role: str):
        xs = pool(role)
        return max(xs, key=lambda cw: (cw[1], cw[0].order))[0] if xs else None

    disc, other, svc, paid, due = best("discount"), best("other"), best("service"), best("paid"), best("due")
    d = disc.value if disc else Decimal(0)
    ch = (other.value if other else Decimal(0)) + (svc.value if svc else Decimal(0))

    subs = pool("subtotal") + [(c, w) for c, w in pool("taxable")]
    taxes = pool("tax")
    comps = [c for c, _ in taxes if c.component]
    if len(comps) >= 2:                              # CGST + SGST: the tax is their sum
        total_tax = sum((c.value for c in comps), Decimal(0))
        virt = Cand(total_tax, str(total_tax), "components", {"tax": 2.9}, comps[0].p, comps[-1].order, False)
        taxes = [x for x in taxes if not x[0].component] + [(virt, 2.9)]
    tots = [(c, max(c.roles.get("total", 0), c.roles.get("due", 0) - 0.3)) for c in cands if c.roles.get("total") or c.roles.get("due")]
    if not tots:
        return {}
    max_val = max(c.value for c, _ in tots)
    top: tuple[float, tuple] | None = None
    for (S, wS), (X, wX), (T, wT) in product(subs + [(None, 0.0)], taxes + [(None, 0.0)], tots):
        if S is not None and S is T or X is not None and X is T:
            continue
        score = wS + wX + wT + 0.4 * T.order + (0.2 if T.value == max_val else 0)
        s_v = S.value if S else None
        if S is not None and S.value > T.value + _tol(T.value):
            score -= 4
        if X is not None and X.value > T.value:
            score -= 4
        if S is not None and X is not None:
            if _fits(S.value, X.value, T.value, d, ch):
                score += 4
            elif _fits(S.value, X.value, T.value, 0, ch):
                score += 3
        elif S is None and X is not None and items_sum is not None:
            if _fits(items_sum, X.value, T.value, d, ch):
                score += 2.5
            elif abs(items_sum - T.value) <= _tol(T.value):      # tax-inclusive lines: tax is "of which"
                score += 2.0
        elif S is not None and X is None:
            if _fits(S.value, Decimal(0), T.value, d, ch):
                score += 1.5
        elif S is None and X is None and items_sum is not None and abs(items_sum - T.value) <= _tol(T.value):
            score += 1.0
        del s_v
        if top is None or score > top[0]:
            top = (score, (S, X, T))
    assert top is not None
    S, X, T = top[1]
    ties = [c for c, w in tots if w >= 2.5 and c is not T]
    if wT_of(T, tots) < 2.0 and any(abs(c.value - T.value) > _tol(T.value) and wT_of(c, tots) >= wT_of(T, tots) - 0.2 for c, _ in tots if c is not T):
        T = None                                     # several "<section> Total" lines: a guess would be wrong silently
    del ties
    out: dict[str, tuple[Cand | None, str]] = {"total": (T, "layout-kv:totals"), "subtotal": (S, "layout-kv:totals"), "tax": (X, "layout-kv:totals")}
    if S is None and X is not None and T is not None and items_sum is not None and _fits(items_sum, X.value, T.value, d, ch):
        out["subtotal"] = (Cand(items_sum, str(items_sum), "items sum", {}, X.p, X.order), "derived:items_sum")
    for k, c in (("discount", disc), ("service_charge", svc), ("other_charges", other), ("amount_paid", paid), ("amount_due", due)):
        if c is not None and c is not T:
            out[k] = (c, "layout-kv:totals")
    return out


def wT_of(c: Cand, tots) -> float:
    return next((w for x, w in tots if x is c), 0.0)


# ------------------------------------------------------------------------------------------------ vendor
def _vendor(rows: list[list[Phrase]], limit_row: int, pairs: list[Pair]) -> RawField:
    region = [p for r in rows[:limit_row] for p in r]
    if not region:
        return RawField(None, 0.0)
    med_h = median(p.h for p in region)
    excluded: set[int] = set()
    forced: list[Phrase] = []
    for pr in pairs:
        words = set(re.findall(r"[a-z]+", pr.label.lower()))
        if words & _CUSTOMER_LABEL and len(words) <= 3:
            excluded.add(id(pr.lp)); excluded.add(id(pr.vp))
            for q in region:                          # lines stacked under a "Bill To" label belong to the customer
                if q.y0 > pr.lp.y0 and q.y0 - pr.lp.y1 <= 5 * pr.lp.h and min(q.x1, pr.lp.x1 + 200) > pr.lp.x0 and abs(q.x0 - pr.lp.x0) < 2 * pr.lp.h:
                    excluded.add(id(q))
        elif words & _VENDOR_LABEL and len(words) <= 3 and pr.how in ("colon", "right", "below"):
            forced.append(pr.vp)
        if (invoice_number_strength(pr.label) or date_strength(pr.label)[1]) and pr.how in ("colon", "right", "typed", "below"):
            first = pr.value.split()[0] if pr.value.split() else ""
            if id_like(first, min_len=1) or find_date(pr.value):      # a real "Invoice No: X" pair, not a title above a name
                excluded.add(id(pr.lp)); excluded.add(id(pr.vp))
    best: tuple[float, Phrase, str] | None = None
    for p in region:
        t = p.text.strip(" -:|'\"._~=—")
        letters = re.sub(r"[^A-Za-z ]", "", t)
        if id(p) in excluded or len(letters.replace(" ", "")) < 3 or _alpha_ratio(t) < 0.6 or title_like(t):
            continue
        if is_money(t) or find_date(t) or _ADDRESS.search(t) or re.match(r"^(to|from|bill|ship|remit|page|date|tel|phone|cliente|client|customer)\b", t, re.I) or ":" in t:
            continue
        # a wrapped name is one block: join lines stacked directly below with the same left edge and size
        text, lines, bottom = t, 1, p
        while True:
            nxt = next((q for q in region if q is not bottom and bottom.cy < q.cy and q.y0 - bottom.y1 <= 0.9 * bottom.h and abs(q.x0 - p.x0) <= 0.6 * p.h and abs(q.h - p.h) <= 0.15 * p.h
                        and not _ADDRESS.search(q.text) and not title_like(q.text) and id(q) not in excluded and _alpha_ratio(q.text) > 0.6), None)
            if nxt is None:
                break
            text += " " + nxt.text.strip(" -:|'\"._~=—"); bottom = nxt; lines += 1
        score = 1.4 * (p.h / med_h) + (1.2 if _LEGAL.search(text) else 0) - 0.25 * sum(1 for q in region if q.y1 <= p.y0 and q is not p) / max(1, len(region)) * 4
        below = [q for q in region if bottom.cy < q.cy and q.y0 - bottom.y1 <= 2.5 * p.h and q.x0 < p.x1]
        if any(_ADDRESS.search(q.text) for q in below):
            score += 0.6
        if p in forced:
            score += 4
        if best is None or score > best[0]:
            best = (score, p, text)
    if best is None:
        return RawField(None, 0.0)
    p = best[1]
    return RawField(best[2], _conf(p, 0.8), page=p.page, bbox=p.bbox, source_text=best[2][:300], method="layout-kv:vendor", ocr_confidence=round(p.conf, 3))


# ------------------------------------------------------------------------------------------------ extractor
class LayoutKVExtractor(LayoutRulesExtractor):
    """Geometry-first extractor. Secondary fields (addresses, phone, email, tax ids, customer, language) still come from
    `layout-rules`; the key fields (vendor, invoice number, date, amounts, line items) come from geometry."""
    name, version = "layout-kv", "0.1.0"

    def extract(self, ocr: OCRResult, pages=None) -> RawExtraction:
        base = super().extract(ocr, pages)
        out = RawExtraction(fields={k: v for k, v in base.fields.items() if k not in ("vendor", "invoice_number", "date", "due_date", "subtotal", "tax", "total",
                                                                                        "discount", "service_charge", "other_charges", "amount_paid", "amount_due", "taxable_amount")},
                            items=[], hints=dict(base.hints))
        pages_rows = [build_rows(p) if p.status == "ok" else [] for p in ocr.pages]
        page_h = {p.number: p.height for p in ocr.pages}

        # tables (per page; a page without a header continues the previous table's columns only through the baseline)
        tables: list[TableParse] = []
        skip: set[tuple[int, int]] = set()
        for pi, rows in enumerate(pages_rows):
            t = parse_table(rows)
            if t is None:
                continue
            tables.append(t)
            for r in range(t.header_row, t.end_row):
                skip.add((pi, r))
        items = [it for t in tables for it in table_to_items(t)]
        out.items = items
        if not items:
            out.items = self._headerless_items(pages_rows, skip) or base.items    # receipts without a header row

        # header fields: rows above the first table, else the upper part of page 1
        rows1 = pages_rows[0] if pages_rows else []
        limit = next((t.header_row for t in tables if t.page == 1), None)
        limit = limit if limit is not None else max(1, int(len(rows1) * 0.6))
        pairs = _pairs(rows1[:limit])
        out.fields["vendor"] = _vendor(rows1, limit, pairs)
        out.fields["invoice_number"] = self._invoice_number(pairs) or self._invoice_number(_pairs(rows1[limit:])) or RawField(None, 0.0)
        for kind in ("date", "due_date"):
            out.fields[kind] = self._date(pairs, kind) or self._date(_pairs(rows1[limit:]), kind) or RawField(None, 0.0)
        if not out.fields["date"].value:
            for r in rows1[:limit]:
                for p in r:
                    if (hit := find_date(p.text)) and not date_strength(p.text[:hit[0]])[0] == "due_date":
                        out.fields["date"] = _rf(hit[1], p, p, 0.7, "layout-kv:first_date"); break
                if out.fields["date"].value:
                    break

        items_sum = None
        if items:
            vals = [_num(i.amount) for i in items if i.amount]
            items_sum = sum((v for v in vals if v is not None), Decimal(0)) if vals else None
        cands = _totals_candidates(pages_rows, skip, page_h, len(pages_rows))
        picked = reconcile(cands, items_sum)
        for name, (c, how) in picked.items():
            if c is None:
                continue
            out.fields[name] = RawField(c.text if how != "derived:items_sum" else str(c.value), min(1.0, c.p.conf * 0.93), page=c.p.page, bbox=c.p.bbox,
                                        source_text=f"{c.label} | {c.text}"[:300], method=how, ocr_confidence=round(c.p.conf, 3))
        for k in ("subtotal", "tax", "total"):
            out.fields.setdefault(k, RawField(None, 0.0))
        return out

    @staticmethod
    def _headerless_items(pages_rows: list[list[list[Phrase]]], skip: set[tuple[int, int]]) -> list[RawItem]:
        """Till receipts: "<name>   <qty> x <price>   <amount>" rows with no header. A row is an item when it starts with
        words, ends with an amount, and its label says nothing about totals, tax or discount."""
        items: list[RawItem] = []
        for pi, rows in enumerate(pages_rows):
            for ri, row in enumerate(rows):
                if (pi, ri) in skip or len(row) < 2 or is_money(row[0].text) or _alpha_ratio(row[0].text) < 0.5 or not is_money(row[-1].text):
                    continue
                if money_roles(row[0].text) or invoice_number_strength(row[0].text) or date_strength(row[0].text)[0] or find_date(row[0].text):
                    continue
                qty = price = None
                mid = row[1:-1]
                joined = " ".join(p.text for p in mid)
                if (m := re.fullmatch(rf"(\d+(?:\.\d+)?)\s*[x×@]\s*(.+)", joined)) and is_money(m[2]):
                    qty, price = m[1], m[2]
                elif len(mid) == 2 and all(is_money(p.text) for p in mid):
                    qty, price = mid[0].text, mid[1].text
                elif mid:
                    continue
                if qty is not None and price is not None:
                    q, pr_, a = _num(qty), _num(price), _num(row[-1].text)
                    if q is None or pr_ is None or a is None or abs(q * pr_ - a) > max(Decimal("0.011"), a * Decimal("0.002")):
                        continue
                bbox = (min(p.x0 for p in row), min(p.y0 for p in row), max(p.x1 for p in row), max(p.y1 for p in row))
                items.append(RawItem(row[0].text.strip(), qty, price, row[-1].text, min(1.0, sum(p.conf for p in row) / len(row) * 0.85),
                                     page=row[0].page, bbox=bbox, source_text=" ".join(p.text for p in row)[:300]))
        return items if len(items) >= 1 else []

    @staticmethod
    def _invoice_number(pairs: Sequence[Pair]) -> RawField | None:
        best: tuple[float, Pair, str] | None = None
        for pr in pairs:
            s = invoice_number_strength(pr.label)
            if s <= 0 or pr.how == "typed" and len(pr.value.split()) > 1:
                if not (pr.how == "typed" and s > 0):
                    continue
            tok = pr.value.split()[0].strip(":,;|") if pr.how == "typed" else pr.value.strip(":,;|")
            if " " in tok.strip() or not id_like(tok, min_len=1 if s >= 3 else 3):
                continue
            if s < 3 and not re.search(r"[A-Za-z]", tok) and len(tok) < 4:
                continue
            score = s - 0.002 * pr.lp.y0 + (0.2 if pr.how in ("colon", "right") else 0)
            if best is None or score > best[0]:
                best = (score, pr, tok.lstrip("#"))
        if best is None:
            return None
        return _rf(best[2], best[1].vp, best[1].lp, 0.95 if best[0] >= 2.5 else 0.8, "layout-kv:label_value")

    @staticmethod
    def _date(pairs: Sequence[Pair], kind: str) -> RawField | None:
        best: tuple[float, Pair, str] | None = None
        for pr in pairs:
            k, s = date_strength(pr.label)
            if k != kind:
                continue
            hit = find_date(pr.value)
            if not hit or (pr.how != "typed" and hit[0] > 3):
                continue
            score = s - 0.002 * pr.lp.y0
            if best is None or score > best[0]:
                best = (score, pr, hit[1])
        if best is None:
            return None
        return _rf(best[2], best[1].vp, best[1].lp, 0.95, "layout-kv:label_value")
