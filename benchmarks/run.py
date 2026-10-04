"""Reproducible benchmark on the SYNTHETIC set.   python -m benchmarks.run --n 72 --seed 7 --out benchmarks/results

Stage 1 (one subprocess per OCR candidate, so peak RSS is per candidate): render -> [preprocess ops] -> OCR,
        OCR results and timings are written to disk.
Stage 2 (this process): every extractor is evaluated on the SAME stored OCR output of each candidate, through the
        production DocumentPipeline (normalisation, validation, fallback).
Output: one immutable JSON file per run (name contains a hash of its contents). Nothing is overwritten.

Synthetic data are rendered from templates the extractors' authors can see. Results rank the candidates on THIS data
only; they say nothing about accuracy on real invoices. See docs/BENCHMARK.md.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

OCR_CANDIDATES = [
    ("tesseract", ""), ("tesseract", "autocontrast"), ("tesseract", "deskew"), ("tesseract", "median3,otsu"),
    ("tesseract", "autocontrast,deskew"), ("rapidocr", ""), ("rapidocr", "deskew"),
]


def _samples(n: int, seed: int):
    from benchmarks.synthetic import generate
    return generate(n, seed=seed)


def stage1(provider: str, ops: str, n: int, seed: int, out: Path) -> None:
    """Runs inside a child process."""
    import resource
    from app.ocr.preprocess_ops import apply_ops, parse_ops
    from app.ocr.providers import RapidOCRProvider, TesseractProvider
    prov = TesseractProvider() if provider == "tesseract" else RapidOCRProvider()
    ops_l = parse_ops(ops)
    t_load = time.perf_counter()
    results, times, errors = [], [], 0
    for s in _samples(n, seed):
        imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in s.images]
        t0 = time.perf_counter()
        try:
            imgs = [apply_ops(i, ops_l) for i in imgs] if ops_l else imgs
            res = prov.recognize(imgs)
            results.append(res.to_dict())
        except Exception:
            errors += 1
            results.append(None)
        times.append((time.perf_counter() - t0) * 1000)
    out.write_text(json.dumps({"provider": provider, "version": prov.version, "ops": ops, "results": results,
                               "ms_per_doc": times, "errors": errors, "total_s": time.perf_counter() - t_load,
                               "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=72)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="benchmarks/results")
    ap.add_argument("--child", nargs=4, metavar=("PROVIDER", "OPS", "OUTFILE", "_"))
    a = ap.parse_args()
    if a.child:
        stage1(a.child[0], a.child[1], a.n, a.seed, Path(a.child[2]))
        return

    from app.extraction.layout_rules import LayoutRulesExtractor
    from app.extraction.rules import RuleBasedExtractor
    from app.ml.evaluation import EvalDoc, evaluate, ReplayOCR, compare
    from app.ocr.base import OCRResult
    from app.ocr.providers import TesseractProvider

    samples = _samples(a.n, a.seed)
    docs_template = [(s, [Image.open(io.BytesIO(b)).convert("RGB") for b in s.images]) for s in samples]
    tmp = Path(tempfile.mkdtemp())
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "synthetic": True, "n": a.n, "seed": a.seed,
              "ocr": {}, "extraction": {}, "comparisons": {}}
    stage_files = {}
    for prov, ops in OCR_CANDIDATES:
        key = f"{prov}|{ops or 'none'}"
        f = tmp / (hashlib.sha1(key.encode()).hexdigest() + ".json")
        t0 = time.time()
        r = subprocess.run([sys.executable, "-m", "benchmarks.run", "--n", str(a.n), "--seed", str(a.seed),
                            "--child", prov, ops, str(f), "x"], capture_output=True, text=True, timeout=3000)
        if r.returncode != 0 or not f.exists():
            report["ocr"][key] = {"status": "failed_to_run", "stderr_tail": r.stderr[-300:]}
            print(key, "FAILED", file=sys.stderr)
            continue
        stage_files[key] = json.loads(f.read_text())
        print(key, "done in", int(time.time() - t0), "s", file=sys.stderr)

    from app.ml import metrics as M
    per_doc_scores: dict[str, dict[str, float]] = {}
    for key, st in stage_files.items():
        from statistics import median
        cers, wers, nums, dts = [], [], [], []
        for s, res in zip(samples, st["results"]):
            if res is None:
                continue
            hyp = "\n".join(ln["text"] for pg in res["pages"] for ln in pg["lines"])
            cers.append(M.cer(s.ocr_text, hyp)); wers.append(M.wer(s.ocr_text, hyp)); nums.append(M.numeric_char_accuracy(s.ocr_text, hyp))
            d = M.date_token_accuracy(s.ocr_text, hyp)
            if d is not None:
                dts.append(d)
        mean = lambda x: sum(x) / len(x) if x else None  # noqa: E731
        ms = st["ms_per_doc"]
        report["ocr"][key] = {"status": "ok", "cer": mean(cers), "wer": mean(wers), "numeric_char_accuracy": mean(nums),
                              "date_token_accuracy": mean(dts), "failures": st["errors"], "docs": len(st["results"]),
                              "latency_ms_p50": M.percentile(ms, 50), "latency_ms_p95": M.percentile(ms, 95),
                              "peak_rss_mb": round(st["peak_rss_mb"], 1), "provider_version": st["version"]}
        # extraction stage on this candidate's stored OCR
        for ename, ex in (("rule-based-baseline", RuleBasedExtractor()), ("layout-rules", LayoutRulesExtractor())):
            docs, replay = [], []
            for (s, imgs), res in zip(docs_template, st["results"]):
                if res is None:
                    continue
                replay.append(OCRResult.from_dict(res))
                docs.append(EvalDoc(s.id, imgs, s.truth, None, {"template": s.template, "degradation": s.degradation}))
            inner = TesseractProvider()
            out = evaluate(docs, ReplayOCR(replay, inner), ex, fallback=RuleBasedExtractor() if ename != "rule-based-baseline" else None)
            ek = f"{key}::{ename}"
            report["extraction"][ek] = {"metrics": out.metrics, "slices": out.slices, "sample_count": out.sample_count,
                                        "per_field": {f: {"f1": v["f1"], "support": v["support"]} for f, v in out.detail["per_field"].items()},
                                        "line_items": out.detail["line_items"], "financial_total": out.detail["financial"].get("total")}
            per_doc_scores[ek] = out.per_document

    # paired comparisons against the two references
    def cmp(a_key: str, b_key: str) -> None:
        if a_key in per_doc_scores and b_key in per_doc_scores:
            report["comparisons"][f"{a_key}  ->  {b_key}"] = compare(per_doc_scores[a_key], per_doc_scores[b_key], seed=a.seed)
    base = "tesseract|none::rule-based-baseline"
    for k in per_doc_scores:
        if k != base:
            cmp(base, k)
    for k in list(per_doc_scores):
        if k.endswith("::layout-rules") and not k.startswith("tesseract|none"):
            cmp("tesseract|none::layout-rules", k)

    body = json.dumps(report, sort_keys=True, default=str, indent=1)
    h = hashlib.sha256(body.encode()).hexdigest()[:12]
    out_dir = Path(a.out); out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{h}.json"
    path.write_text(body)
    print(path)


if __name__ == "__main__":
    main()
