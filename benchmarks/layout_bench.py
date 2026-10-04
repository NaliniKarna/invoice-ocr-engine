"""Compare extraction strategies across many invoice layouts, with OCR and extraction measured separately.

    python -m benchmarks.layout_bench --out benchmarks/results --per-layout 3

Modes
  oracle   exact word boxes from the browser DOM (perfect OCR)  -> failures here are extraction/layout failures
  real     Tesseract on the rendered PNG (production defaults)   -> failures here = extraction + OCR
  lowres   same image shrunk to ~560 px wide and JPEG q35        -> image-quality stress
The "OCR loss" of a strategy is oracle score minus real score.

All documents are SYNTHETIC and drawn by the extractor's author. Layouts 1-6 are DEV, 7-12 are HELD OUT. Numbers rank
strategies on these layouts only. They are not real-world accuracy.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PIL import Image

from benchmarks import layouts as L

FIELDS = ("vendor", "invoice_number", "date", "subtotal", "tax", "total", "currency")
CRITICAL = ("vendor", "invoice_number", "date", "subtotal", "tax", "total")


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _dec(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return None


def score_doc(data: dict, truth: dict) -> dict:
    """1/0 per field present in the truth; item_f1 = F1 of line amounts (multiset)."""
    out: dict[str, float] = {}
    for f in FIELDS:
        t = truth.get(f)
        if t is None:
            continue
        v = data.get(f)
        if f in ("subtotal", "tax", "total"):
            a, b = _dec(v), _dec(t)
            ok = a is not None and b is not None and abs(a - b) <= Decimal("0.005")
        elif f == "vendor":
            ok = _norm(v) == _norm(t)
        elif f == "invoice_number":
            ok = _norm(v) == _norm(t)
        else:
            ok = str(v) == str(t)
        out[f] = 1.0 if ok else 0.0
    got = sorted(str(Decimal(str(i["amount"])).quantize(Decimal("0.01"))) for i in data.get("items", []) if i.get("amount") is not None)
    want = list(truth["item_amounts"])
    rest, tp = list(want), 0
    for g in got:
        if g in rest:
            rest.remove(g); tp += 1
    p, r = (tp / len(got) if got else 0.0), tp / len(want)
    out["item_f1"] = 0.0 if p + r == 0 else 2 * p * r / (p + r)
    out["critical_all"] = 1.0 if all(out.get(f) == 1.0 for f in CRITICAL if f in out) else 0.0
    return out


# ------------------------------------------------------------------------------------------------ OCR modes
def prepare_image(png: bytes, mode: str) -> Image.Image:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if mode == "lowres":
        w = 560
        im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=35); im = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    return im


def real_ocr(png: bytes, mode: str):
    from app.ocr.providers import TesseractProvider
    return TesseractProvider().recognize([prepare_image(png, mode)])


PIPELINES: dict = {}          # name -> callable(images, oracle_ocr_or_None) -> (PipelineOutput)


def pipeline(name):
    def deco(fn):
        PIPELINES[name] = fn
        return fn
    return deco


STRATEGIES: dict = {}
PIPE_PRIMARY: dict = {}


def strategy(name):
    def deco(fn):
        STRATEGIES[name] = fn
        return fn
    return deco


def _single(extractor_factory):
    def run(ocr, images):
        from app.extraction.postprocess import build_output
        raw = extractor_factory().extract(ocr, images)
        return build_output(raw, day_first=True, review_threshold=0.8)
    return run


@strategy("regex-baseline")
def _s_rule(ocr, images):
    from app.extraction.rules import RuleBasedExtractor
    return _single(RuleBasedExtractor)(ocr, images)


@strategy("layout-rules")
def _s_lr(ocr, images):
    from app.extraction.layout_rules import LayoutRulesExtractor
    return _single(LayoutRulesExtractor)(ocr, images)


def _data(out) -> dict:
    d = out.data.model_dump(mode="json")
    return d


def run(per_layout: int, names: list[str], modes: list[str], layouts: tuple[int, ...] | None = None) -> dict:
    docs = L.generate(per_layout, layouts=layouts)
    results: dict = {}
    ocr_cache: dict = {}
    for mode in modes:
        for doc in docs:
            if mode == "oracle":
                ocr = L.oracle_ocr(doc)
            else:
                ocr = ocr_cache.setdefault((mode, doc["id"]), real_ocr(doc["png"], mode))
            images = [prepare_image(doc["png"], "real" if mode == "oracle" else mode)]
            for name in names:
                try:
                    po = None
                    if name in PIPELINES:
                        po = PIPELINES[name](images, ocr if mode == "oracle" else None)
                        out = po.extraction
                    else:
                        out = STRATEGIES[name](ocr, images)
                    sc = score_doc(_data(out), doc["truth"])
                    sc["requires_review"] = 1.0 if out.requires_review else 0.0
                    sc["confidence"] = out.confidence
                    detail = _data(out)
                    if po is not None:
                        sc["retry_used"] = 1.0 if (po.retry and po.retry.get("used")) else 0.0
                        sc["retry_tried"] = 1.0 if po.retry else 0.0
                        sc["alt_used"] = 1.0 if po.extractor.name != PIPE_PRIMARY.get(name, po.extractor.name) else 0.0
                except Exception as exc:                      # a crash counts as all-wrong (and is printed)
                    print("CRASH", name, doc["id"], repr(exc), file=sys.stderr)
                    sc, detail = {**{f: 0.0 for f in FIELDS}, "item_f1": 0.0, "critical_all": 0.0, "requires_review": 1.0, "confidence": 0.0}, {"error": repr(exc)}
                results.setdefault(name, {}).setdefault(mode, {})[doc["id"]] = {"layout": doc["layout"], "split": doc["split"], "score": sc,
                                                                                  "got": {k: detail.get(k) for k in ("vendor", "invoice_number", "date", "subtotal", "tax", "total", "currency")} | {"n_items": len(detail.get("items", []))}, "truth": doc["truth"]}
    return results


def summarize(results: dict) -> str:
    lines = []
    for name, modes in results.items():
        for mode, docs in modes.items():
            for split in ("dev", "held_out", "round2"):
                rows = [d["score"] for d in docs.values() if d["split"] == split]
                if not rows:
                    continue
                cells = []
                for f in (*FIELDS, "item_f1", "critical_all"):
                    v = [r[f] for r in rows if f in r]
                    cells.append(f"{f}={sum(v) / len(v):.2f}" if v else f"{f}=-")
                lines.append(f"{name:16s} {mode:7s} {split:8s} n={len(rows):2d}  " + "  ".join(cells))
    return "\n".join(lines)


def per_layout(results: dict, name: str, mode: str) -> str:
    docs = results[name][mode]
    out = []
    for lid in sorted({d["layout"] for d in docs.values()}):
        rows = [d["score"] for d in docs.values() if d["layout"] == lid]
        out.append(f"L{lid:02d} crit={sum(r['critical_all'] for r in rows) / len(rows):.2f} " + " ".join(
            f"{f[:4]}={sum(r[f] for r in rows if f in r) / max(1, sum(1 for r in rows if f in r)):.1f}" for f in (*FIELDS[:6], "item_f1")))
    return "\n".join(out)


def bootstrap(results: dict, a: str, b: str, mode: str, split: str, metric="critical_all", n=2000, seed=1) -> tuple[float, float, float]:
    """Paired bootstrap on per-document scores: mean(b-a) with a 95% interval. With ~18 docs per split this is wide."""
    ids = [i for i, d in results[a][mode].items() if d["split"] == split]
    diffs = [results[b][mode][i]["score"][metric] - results[a][mode][i]["score"][metric] for i in ids]
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n))
    return sum(diffs) / len(diffs), means[int(0.025 * n)], means[int(0.975 * n)]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-layout", type=int, default=3)
    ap.add_argument("--strategies", default="regex-baseline,layout-rules,layout-kv")
    ap.add_argument("--modes", default="oracle,real,lowres")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--round2", action="store_true", help="run only the frozen round-2 layouts 13-18")
    ap.add_argument("--detail", default="", help="print per-layout lines for this strategy")
    a = ap.parse_args()
    import benchmarks.layout_strategies  # noqa: F401  (registers the new strategies)
    from benchmarks import layout_bench as lb          # this file runs as __main__: use the module the strategies registered into
    res = lb.run(a.per_layout, a.strategies.split(","), a.modes.split(","), lb.L.ROUND2 if a.round2 else None)
    print(lb.summarize(res))
    if a.detail:
        for m in a.modes.split(","):
            print(f"\n[{a.detail} / {m}]"); print(lb.per_layout(res, a.detail, m))
    if a.json:
        a.json.write_text(json.dumps(res, default=str))
