"""Evaluation metrics. Pure Python, no I/O, deterministic.

Conventions
* Text distance is Levenshtein over characters (CER) or whitespace-separated words (WER), computed with the
  bit-parallel algorithm of Myers/Hyyro, which is exact and fast for long strings.
* Field metrics count, per field: TP (truth present and prediction equal), FP (prediction present and wrong or truth
  absent), FN (truth present and prediction missing or wrong). A wrong value is therefore both an FP and an FN.
* "exact" compares the stored strings; "normalized" compares after canonicalisation (case/space/punctuation for text,
  Decimal equality for money, ISO date equality for dates).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Hashable, Sequence

MONEY = ("subtotal", "discount", "taxable_amount", "tax", "service_charge", "other_charges", "total", "amount_paid", "amount_due")
DATES = ("date", "due_date")
TEXT = ("vendor", "supplier_address", "supplier_phone", "supplier_email", "customer_name", "customer_address")
IDS = ("invoice_number", "document_number", "supplier_tax_id", "customer_id", "customer_tax_id")
ENUMS = ("currency", "language", "document_type")
ALL_FIELDS = (*TEXT, *IDS, *DATES, *ENUMS, *MONEY)
CRITICAL = ("vendor", "invoice_number", "date", "total")          # document-level success requires all four
FINANCIAL = ("subtotal", "discount", "taxable_amount", "tax", "service_charge", "other_charges", "total", "amount_paid", "amount_due")


# ------------------------------------------------------------------ text distance
def edit_distance(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """Exact Levenshtein distance (bit-parallel, Hyyro 2003)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    m = len(a)
    full = (1 << m) - 1
    peq: dict[Hashable, int] = {}
    for i, c in enumerate(a):
        peq[c] = peq.get(c, 0) | (1 << i)
    pv, mv, score, last = full, 0, m, 1 << (m - 1)
    for c in b:
        eq = peq.get(c, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & full) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & full
        mh = pv & xh
        if ph & last:
            score += 1
        elif mh & last:
            score -= 1
        ph = ((ph << 1) | 1) & full
        mh = (mh << 1) & full
        pv = (mh | ~(xv | ph)) & full
        mv = ph & xv
    return score


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def cer(truth: str, hyp: str) -> float:
    t, h = _norm_ws(truth), _norm_ws(hyp)
    return edit_distance(t, h) / max(len(t), 1)


def wer(truth: str, hyp: str) -> float:
    t, h = _norm_ws(truth).split(), _norm_ws(hyp).split()
    return edit_distance(t, h) / max(len(t), 1)


def numeric_char_accuracy(truth: str, hyp: str) -> float:
    """1 - distance between the digit-and-separator streams of truth and hypothesis (amounts, dates, ids)."""
    t = re.sub(r"[^0-9.,/\-]", "", truth)
    h = re.sub(r"[^0-9.,/\-]", "", hyp)
    return max(0.0, 1 - edit_distance(t, h) / max(len(t), 1))


_DATE_TOKEN = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")


def date_token_accuracy(truth: str, hyp: str) -> float | None:
    """Share of date tokens in the ground-truth text that appear verbatim in the OCR text (None if no dates)."""
    toks = _DATE_TOKEN.findall(truth)
    if not toks:
        return None
    hyp_ns = hyp.replace(" ", "")
    return sum(1 for t in toks if t in hyp_ns) / len(toks)


# ------------------------------------------------------------------ value comparison
def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _text_key(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(v).casefold()).strip()


def _id_key(v: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(v).casefold())


def values_equal(field: str, truth: Any, pred: Any, *, normalized: bool = True) -> bool:
    if truth in (None, "") or pred in (None, ""):
        return truth in (None, "") and pred in (None, "")
    if not normalized:
        return str(truth) == str(pred)
    if field in MONEY:
        a, b = _dec(truth), _dec(pred)
        return a is not None and b is not None and a == b
    if field in DATES:
        return str(truth)[:10] == str(pred)[:10]
    if field in IDS:
        return _id_key(truth) == _id_key(pred)
    return _text_key(truth) == _text_key(pred)


def _prf(tp: int, fp: int, fn: int) -> dict[str, float | None]:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None or r is not None) and tp == 0 and (fp or fn) else None)
    return {"precision": p, "recall": r, "f1": f}


def field_metrics(pairs: Sequence[tuple[dict, dict]], fields: Sequence[str] = ALL_FIELDS) -> dict:
    """pairs: [(truth, prediction)] of InvoiceData-shaped dicts. Returns per-field and micro-averaged P/R/F1 and accuracy."""
    per: dict[str, dict] = {}
    tot = defaultdict(int)
    for f in fields:
        tp = fp = fn = exact = support = 0
        for truth, pred in pairs:
            t, p = truth.get(f), pred.get(f)
            if t in (None, "") and p in (None, ""):
                continue
            if t not in (None, ""):
                support += 1
            if values_equal(f, t, p):
                tp += 1
                exact += values_equal(f, t, p, normalized=False)
            else:
                if p not in (None, ""):
                    fp += 1
                if t not in (None, ""):
                    fn += 1
        if support or fp:
            per[f] = {"support": support, "tp": tp, "fp": fp, "fn": fn, "exact_match": exact,
                      "normalized_accuracy": (tp / support) if support else None, **_prf(tp, fp, fn)}
            tot["tp"] += tp; tot["fp"] += fp; tot["fn"] += fn; tot["support"] += support; tot["exact"] += exact
    micro = {"support": tot["support"], "exact_match": tot["exact"], **_prf(tot["tp"], tot["fp"], tot["fn"]),
             "normalized_accuracy": (tot["tp"] / tot["support"]) if tot["support"] else None}
    return {"per_field": per, "micro": micro}


def financial_metrics(pairs: Sequence[tuple[dict, dict]], tol_abs: Decimal = Decimal("0.01"),
                      tol_rel: Decimal = Decimal("0")) -> dict:
    """Exact accuracy, tolerance accuracy, and absolute/relative error for money fields present in the truth."""
    per: dict[str, dict] = {}
    for f in FINANCIAL:
        n = exact = within = missing = 0
        abs_err: list[Decimal] = []
        rel_err: list[Decimal] = []
        for truth, pred in pairs:
            t = _dec(truth.get(f))
            if t is None:
                continue
            n += 1
            p = _dec(pred.get(f))
            if p is None:
                missing += 1
                continue
            d = abs(p - t)
            abs_err.append(d)
            if t != 0:
                rel_err.append(d / abs(t))
            exact += d == 0
            within += d <= max(tol_abs, tol_rel * abs(t))
        if n:
            per[f] = {"support": n, "exact_accuracy": exact / n, "tolerance_accuracy": within / n, "missing": missing,
                      "mean_abs_error": float(sum(abs_err) / len(abs_err)) if abs_err else None,
                      "mean_rel_error": float(sum(rel_err) / len(rel_err)) if rel_err else None}
    return per


def date_metrics(pairs: Sequence[tuple[dict, dict]]) -> dict:
    out = {}
    for f in DATES:
        n = ok = 0
        for truth, pred in pairs:
            if truth.get(f):
                n += 1; ok += values_equal(f, truth[f], pred.get(f))
        if n:
            out[f] = {"support": n, "normalized_exact_accuracy": ok / n}
    return out


# ------------------------------------------------------------------ line items
def _sim(a: str, b: str) -> float:
    a, b = _text_key(a), _text_key(b)
    return 1 - edit_distance(a, b) / max(len(a), len(b), 1)


def match_items(truth: list[dict], pred: list[dict], threshold: float = 0.8) -> list[tuple[int, int]]:
    """One-to-one greedy matching on description similarity (amount equality breaks ties)."""
    cands = []
    for i, t in enumerate(truth):
        for j, p in enumerate(pred):
            s = _sim(t.get("description") or "", p.get("description") or "")
            same_amt = values_equal("total", t.get("amount"), p.get("amount")) and t.get("amount") not in (None, "")
            score = s + (0.25 if same_amt else 0)
            if s >= threshold or (same_amt and s >= 0.4):
                cands.append((score, i, j))
    cands.sort(reverse=True)
    used_i: set[int] = set(); used_j: set[int] = set(); out = []
    for _, i, j in cands:
        if i not in used_i and j not in used_j:
            used_i.add(i); used_j.add(j); out.append((i, j))
    return out


ITEM_FIELDS = ("description", "quantity", "unit_price", "amount")


def line_item_metrics(pairs: Sequence[tuple[dict, dict]]) -> dict:
    tp = fp = fn = 0
    fa = {f: [0, 0] for f in ITEM_FIELDS}          # [correct, total] over matched rows
    full_rows = truth_rows = 0
    count_ok = docs = 0
    for truth, pred in pairs:
        ti, pi = truth.get("items") or [], pred.get("items") or []
        if not ti and not pi:
            continue
        docs += 1
        count_ok += len(ti) == len(pi)
        m = match_items(ti, pi)
        tp += len(m); fp += len(pi) - len(m); fn += len(ti) - len(m)
        truth_rows += len(ti)
        for i, j in m:
            ok_all = True
            for f in ITEM_FIELDS:
                if f == "description":
                    ok = _text_key(ti[i].get(f) or "") == _text_key(pi[j].get(f) or "")
                else:
                    ok = values_equal("total", ti[i].get(f), pi[j].get(f))
                fa[f][0] += ok; fa[f][1] += 1
                ok_all &= ok
            full_rows += ok_all
    return {"documents": docs, "rows_truth": truth_rows, "row_detection": _prf(tp, fp, fn),
            "field_accuracy": {f: (c / n if n else None) for f, (c, n) in fa.items()},
            "aggregate_row_accuracy": (full_rows / truth_rows) if truth_rows else None,
            "item_count_exact": (count_ok / docs) if docs else None}


def document_metrics(pairs: Sequence[tuple[dict, dict]], failures: int = 0) -> dict:
    n = len(pairs) + failures
    crit = full = 0
    for truth, pred in pairs:
        crit += all(values_equal(f, truth.get(f), pred.get(f)) for f in CRITICAL if truth.get(f) not in (None, ""))
        full += all(values_equal(f, truth.get(f), pred.get(f)) for f in ALL_FIELDS if truth.get(f) not in (None, ""))
    return {"documents": n, "failed": failures, "failure_rate": failures / n if n else None,
            "critical_fields_correct": crit / n if n else None, "all_present_fields_correct": full / n if n else None}


# ------------------------------------------------------------------ calibration
def reliability(confidences: Sequence[float], correct: Sequence[bool], bins: int = 10) -> dict:
    """Reliability table, Expected Calibration Error (ECE) and Brier score. Equal-width bins on [0, 1]."""
    if len(confidences) != len(correct):
        raise ValueError("length mismatch")
    n = len(confidences)
    if n == 0:
        return {"n": 0, "ece": None, "brier": None, "bins": []}
    rows = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(confidences) if (lo <= c < hi) or (b == bins - 1 and c == 1.0)]
        if not idx:
            continue
        conf = sum(confidences[i] for i in idx) / len(idx)
        acc = sum(1 for i in idx if correct[i]) / len(idx)
        rows.append({"lo": lo, "hi": hi, "n": len(idx), "mean_confidence": conf, "accuracy": acc})
        ece += len(idx) / n * abs(acc - conf)
    brier = sum((c - (1.0 if ok else 0.0)) ** 2 for c, ok in zip(confidences, correct)) / n
    return {"n": n, "ece": ece, "brier": brier, "bins": rows}


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0          # q in percent (0-100)
    f, c = math.floor(k), math.ceil(k)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)
