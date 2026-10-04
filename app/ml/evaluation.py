"""Evaluation harness: runs OCR and/or extraction on labelled documents and computes metrics.

Design rules
* Results are plain data. Persisting an EvaluationRun is an INSERT into an immutable table (see models.ml).
* Comparison is statistical, never subjective: a paired bootstrap on per-document scores yields a verdict of
  a_better / b_better / inconclusive / insufficient_data. Small data is reported as such, not ranked.
* Nothing here is synthetic-aware on its own: callers mark a run `synthetic=True` and the registry gates then refuse
  to approve a model on it.
"""
from __future__ import annotations

import hashlib
import json
import platform
import random
import resource
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from PIL import Image

from app.extraction.base import Extractor
from app.extraction.pipeline import DocumentPipeline
from app.extraction.schema import SCHEMA_VERSION
from app.extraction.validation import VALIDATION_VERSION
from app.ml import metrics as M
from app.ocr.base import OCRProvider, OCRResult

EVAL_HARNESS_VERSION = "1"
MIN_DOCS_FOR_COMPARISON = 30      # below this a comparison is reported as insufficient_data


@dataclass
class EvalDoc:
    id: str
    images: list[Image.Image]
    truth: dict
    ocr_text: str | None = None           # exact page text when known (synthetic sets); enables CER / WER
    slices: dict[str, str] = field(default_factory=dict)   # e.g. {"template": "nepal", "degradation": "blur"}


class ReplayOCR(OCRProvider):
    """Returns pre-computed OCR results so several extractors can be compared on identical OCR output."""
    name, version = "replay", "1"

    def __init__(self, results: list[OCRResult], inner: OCRProvider):
        self.results, self.i, self.inner = results, 0, inner
        self.name, self.version = inner.name, inner.version

    def recognize(self, pages):  # type: ignore[override]
        r = self.results[self.i]
        self.i += 1
        return r


@dataclass
class EvalOutput:
    metrics: dict
    detail: dict
    slices: dict
    per_document: dict[str, float]
    sample_count: int
    dataset_sha256: str
    environment: dict
    config: dict


def dataset_fingerprint(docs: Sequence[EvalDoc]) -> str:
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda x: x.id):
        h.update(d.id.encode()); h.update(json.dumps(d.truth, sort_keys=True, default=str).encode())
    return h.hexdigest()


def environment_info() -> dict:
    import importlib.metadata as md
    libs = {}
    for lib in ("numpy", "pillow", "pydantic", "pytesseract", "rapidocr-onnxruntime", "nepali-datetime"):
        try:
            libs[lib] = md.version(lib)
        except md.PackageNotFoundError:
            pass
    from app.core.version import APP_VERSION, code_fingerprint
    return {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
            "app_version": APP_VERSION, "code_sha256": code_fingerprint(), "libraries": libs, "harness_version": EVAL_HARNESS_VERSION, "schema_version": SCHEMA_VERSION,
            "validation_version": VALIDATION_VERSION}


def peak_rss_mb() -> float:
    """Process-wide high-water mark (Linux: KiB). Isolate candidates in separate processes for a per-candidate figure."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _doc_score(truth: dict, pred: dict) -> float:
    present = [f for f in M.ALL_FIELDS if truth.get(f) not in (None, "")]
    if not present:
        return 1.0
    return sum(M.values_equal(f, truth.get(f), pred.get(f)) for f in present) / len(present)


def evaluate(docs: Sequence[EvalDoc], ocr: OCRProvider, extractor: Extractor, *, day_first: bool = True,
             review_threshold: float = 0.8, fallback: Extractor | None = None,
             tolerance_abs: Decimal = Decimal("0.01"), config: dict | None = None) -> EvalOutput:
    pipe = DocumentPipeline(ocr, extractor, day_first=day_first, review_threshold=review_threshold, fallback=fallback)
    pairs: list[tuple[dict, dict]] = []
    per_doc: dict[str, float] = {}
    lat_ocr: list[float] = []
    lat_ext: list[float] = []
    cers: list[float] = []; wers: list[float] = []; nums: list[float] = []; dates: list[float] = []
    conf: list[float] = []; correct: list[bool] = []
    failures = 0
    fallbacks = 0
    by_slice: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for d in docs:
        try:
            out = pipe.run(d.images)
        except Exception:     # any failure counts against the candidate; the error text is deliberately not retained
            failures += 1
            per_doc[d.id] = 0.0
            continue
        pred = out.extraction.data.model_dump(mode="json")
        pairs.append((d.truth, pred))
        per_doc[d.id] = _doc_score(d.truth, pred)
        lat_ocr.append(out.ocr_ms); lat_ext.append(out.extraction_ms)
        if out.fallback_from:
            fallbacks += 1
        if d.ocr_text:
            hyp = "\n".join(ln.text for pg in out.ocr.pages for ln in pg.lines)
            cers.append(M.cer(d.ocr_text, hyp)); wers.append(M.wer(d.ocr_text, hyp))
            nums.append(M.numeric_char_accuracy(d.ocr_text, hyp))
            da = M.date_token_accuracy(d.ocr_text, hyp)
            if da is not None:
                dates.append(da)
        # confidence vs. whether every critical field present in the truth was right (for reliability / ECE)
        crit_ok = all(M.values_equal(f, d.truth.get(f), pred.get(f)) for f in M.CRITICAL if d.truth.get(f) not in (None, ""))
        conf.append(out.extraction.confidence); correct.append(crit_ok)
        for k, v in d.slices.items():
            by_slice.setdefault((k, v), []).append((d.truth, pred))

    fm = M.field_metrics(pairs)
    crit = M.field_metrics(pairs, M.CRITICAL)
    fin = M.financial_metrics(pairs, tolerance_abs)
    li = M.line_item_metrics(pairs)
    dm = M.date_metrics(pairs)
    doc = M.document_metrics(pairs, failures)
    rel = M.reliability(conf, correct) if conf else {}
    total_ex = fin.get("total", {}).get("exact_accuracy")
    date_ex = dm.get("date", {}).get("normalized_exact_accuracy")
    mean = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    metrics = {
        "field_f1": fm["micro"]["f1"], "field_precision": fm["micro"]["precision"], "field_recall": fm["micro"]["recall"],
        "critical_field_f1": crit["micro"]["f1"], "total_exact": total_ex, "date_exact": date_ex,
        "line_item_f1": li["row_detection"]["f1"], "line_item_row_accuracy": li["aggregate_row_accuracy"],
        "documents_critical_correct": doc["critical_fields_correct"], "failure_rate": doc["failure_rate"],
        "fallback_rate": (fallbacks / len(docs)) if docs else None,
        "cer": mean(cers), "wer": mean(wers), "numeric_char_accuracy": mean(nums), "date_token_accuracy": mean(dates),
        "latency_ocr_p50_ms": M.percentile(lat_ocr, 50), "latency_ocr_p95_ms": M.percentile(lat_ocr, 95),
        "latency_extract_p50_ms": M.percentile(lat_ext, 50), "latency_extract_p95_ms": M.percentile(lat_ext, 95),
        "latency_p95_ms": M.percentile([a + b for a, b in zip(lat_ocr, lat_ext)], 95),
        "ece_provisional": rel.get("ece"), "brier_provisional": rel.get("brier"), "peak_rss_mb": peak_rss_mb(),
    }
    slices = {f"{k}={v}": {"n": len(p), "field_f1": M.field_metrics(p)["micro"]["f1"],
                           "critical_field_f1": M.field_metrics(p, M.CRITICAL)["micro"]["f1"]}
              for (k, v), p in sorted(by_slice.items())}
    cfg = {"ocr": f"{ocr.name}@{ocr.version}", "extractor": f"{extractor.name}@{extractor.version}",
           "tolerance_abs": str(tolerance_abs), "day_first": day_first, **(config or {})}
    return EvalOutput(metrics=metrics, detail={"per_field": fm["per_field"], "financial": fin, "line_items": li, "dates": dm,
                                               "reliability": rel},
                      slices=slices, per_document=per_doc, sample_count=len(docs), dataset_sha256=dataset_fingerprint(docs),
                      environment=environment_info(), config=cfg)


# ------------------------------------------------------------------ persistence
def persist_run(db, model_version_id: str, out: EvalOutput, *, dataset_ref: str, split: str = "test", synthetic: bool,
                dataset_id: str | None = None, created_by: str = "admin"):
    from app.core.ids import new_id
    from app.models import EvaluationRun
    from app.observability import metrics as pm
    payload = {"metrics": out.metrics, "slices": out.slices, "detail": out.detail, "config": out.config}
    sha = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    run = EvaluationRun(
        id=new_id("eval"), model_version_id=model_version_id, dataset_id=dataset_id, dataset_ref=dataset_ref[:200],
        dataset_sha256=out.dataset_sha256, split=split, sample_count=out.sample_count, metrics=json.loads(json.dumps(out.metrics, default=str)),
        slices={**json.loads(json.dumps(out.slices, default=str)), "_per_document": out.per_document},
        config={**out.config, "detail": json.loads(json.dumps(out.detail, default=str))},
        environment=out.environment, synthetic=synthetic, result_sha256=sha, created_by=created_by)
    db.add(run)
    db.flush()
    pm.ML_EVALUATIONS.labels(kind="extractor", synthetic=str(synthetic).lower()).inc()
    return run


# ------------------------------------------------------------------ comparison
def compare(a: dict[str, float], b: dict[str, float], *, seed: int = 0, n_boot: int = 2000, alpha: float = 0.05) -> dict:
    """Paired bootstrap of mean(b - a) over documents present in both. Verdict is statistical, never a subjective ranking."""
    ids = sorted(set(a) & set(b))
    n = len(ids)
    if n < MIN_DOCS_FOR_COMPARISON:
        return {"n": n, "verdict": "insufficient_data",
                "note": f"{n} shared documents < {MIN_DOCS_FOR_COMPARISON}: a difference cannot be distinguished from noise"}
    diffs = [b[i] - a[i] for i in ids]
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo, hi = means[int(alpha / 2 * n_boot)], means[int((1 - alpha / 2) * n_boot) - 1]
    verdict = "b_better" if lo > 0 else "a_better" if hi < 0 else "inconclusive"
    return {"n": n, "mean_delta": sum(diffs) / n, "ci_low": lo, "ci_high": hi, "confidence": 1 - alpha, "verdict": verdict}
