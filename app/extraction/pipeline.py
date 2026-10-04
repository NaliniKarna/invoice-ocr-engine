"""pages -> OCR -> (layout) -> extraction -> normalisation -> validation -> confidence. Pure (no DB/storage).

Stages are separate objects so each can be replaced and measured on its own:
    OCRProvider.recognize  -> OCRResult (pages, lines, word boxes, per-page status)
    Extractor.extract      -> RawExtraction (strings + evidence; layout analysis is the extractor's concern)
    build_output           -> normalisation (with provenance), validation, heuristic confidence
Safety rules implemented here:
* a page that could not be rendered or recognised is recorded as failed and forces human review; it is never dropped silently;
* extractor output is validated against the canonical schema; a crash or invalid output triggers the reference
  fallback extractor, and the result records which extractor really produced it (`extractor` / `fallback_from`);
* if nothing produced valid output the job fails (ExtractionError) instead of returning partial data as authoritative.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Sequence

from PIL import Image
from pydantic import ValidationError

from app.extraction.base import Extractor, RawExtraction
from app.extraction.postprocess import build_output
from app.extraction.schema import ExtractionOutput, ValidationIssue
from app.extraction.validation import GATE_CODES, ValidationConfig
from app.observability import metrics as m
from app.observability import tracing
from app.ocr.base import ExtractionError, OCRPage, OCRPermanentError, OCRProvider, OCRResult
from app.ocr.preprocess_ops import apply_ops

log = logging.getLogger(__name__)

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_LETTER = re.compile(r"[^\W\d_]")
UNSUPPORTED_SCRIPT_MIN_CHARS = 10
UNSUPPORTED_SCRIPT_MIN_RATIO = 0.10


def unsupported_script_issue(ocr: OCRResult) -> ValidationIssue | None:
    """Devanagari text is NOT supported: neither extractor, nor normalisation (apart from digits), nor the default OCR
    configuration (English Tesseract) handles it. If the engine emitted Devanagari characters, say so and force review.
    Limit: an English-only engine does not emit Devanagari; it emits Latin-looking noise instead, which this check cannot
    see. Detecting that case is not implemented (see docs/RELEASE_AUDIT.md)."""
    text = "\n".join(ln.text for pg in ocr.pages for ln in pg.lines)
    dev = len(_DEVANAGARI.findall(text))
    letters = len(_LETTER.findall(text))
    if dev >= UNSUPPORTED_SCRIPT_MIN_CHARS and letters and dev / letters >= UNSUPPORTED_SCRIPT_MIN_RATIO:
        return ValidationIssue(code="unsupported_script", field=None, severity="error",
                               message="The document contains Devanagari text, which is not supported; the extracted data is unreliable")
    return None


@dataclass
class RetryPlan:
    """Second OCR pass: another provider configuration (e.g. Tesseract page mode 6) on pages with extra preprocessing ops."""
    ocr: OCRProvider
    ops: list[str]
    below_confidence: float = 0.5
    max_pages: int = 3
    psm: int | None = None          # recorded for provenance only
    on_key_fields: bool = True      # also retry when a key field is missing or suspicious, even if confidence is high


# Issues a different image treatment cannot fix: do not spend a second pass on them.
_NO_RETRY_CODES = {"unsupported_script", "page_ocr_failed"}


@dataclass
class PipelineOutput:
    ocr: OCRResult
    extraction: ExtractionOutput
    ocr_ms: int
    extraction_ms: int
    extractor: Extractor
    fallback_from: str | None = None          # "name@version" of the primary extractor when the fallback produced this
    failed_pages: list[int] = field(default_factory=list)
    retry: dict | None = None                 # provenance of the extra OCR passes: {"attempted", "used", "passes": [...], "first": {...}}
    strategies: list[dict] | None = None      # provenance of alternate extractors tried on this OCR result


class DocumentPipeline:
    def __init__(self, ocr: OCRProvider, extractor: Extractor, *, day_first: bool, review_threshold: float,
                 fallback: Extractor | None = None, validation: ValidationConfig | None = None,
                 fail_on_partial: bool = False, retry: "RetryPlan | Sequence[RetryPlan] | None" = None,
                 alternates: Sequence[Extractor] = ()):
        self.ocr, self.extractor, self.fallback = ocr, extractor, fallback
        self.retries: list[RetryPlan] = [] if retry is None else [retry] if isinstance(retry, RetryPlan) else list(retry)
        self.retry = self.retries[0] if self.retries else None       # thresholds come from the first variant
        self.alternates = [a for a in alternates if a is not extractor]
        self.day_first, self.review_threshold = day_first, review_threshold
        self.validation, self.fail_on_partial = validation, fail_on_partial

    # -- OCR with page-level bookkeeping
    def _recognize(self, pages: list[Image.Image | None], ocr: OCRProvider | None = None) -> OCRResult:
        present = [(i, p) for i, p in enumerate(pages, start=1) if p is not None]
        res = (ocr or self.ocr).recognize([p for _, p in present])
        by_number = {orig: pg for (orig, _), pg in zip(present, res.pages)}
        out: list[OCRPage] = []
        for i, p in enumerate(pages, start=1):
            if p is None:
                out.append(OCRPage(i, 0, 0, [], status="failed", error_code="page_render_failed"))
            else:
                pg = by_number[i]; pg.number = i
                out.append(pg)
        return OCRResult(res.provider, res.model_version, out)

    def _extract(self, extractor: Extractor, ocr: OCRResult, pages) -> tuple[RawExtraction, ExtractionOutput]:
        raw = extractor.extract(ocr, pages)
        if not isinstance(raw, RawExtraction):
            raise ExtractionError("extractor returned an unexpected type")
        try:
            out = build_output(raw, day_first=self.day_first, review_threshold=self.review_threshold,
                               ocr_quality=1.0, cfg=self.validation)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ExtractionError("extractor output failed schema validation") from exc
        return raw, out

    @staticmethod
    def _gate_problems(ex: ExtractionOutput) -> list[str]:
        """Key-field problems (missing/suspicious vendor, number, date, total, unexplained totals). Overall confidence averages four
        fields and can stay above any threshold while one of them is wrong, so the decision to try something else reads these."""
        return sorted({i.code for i in ex.issues if i.code in GATE_CODES})

    @staticmethod
    def _filled(ex: ExtractionOutput) -> int:
        """How many of the key outputs were found. Lets a result that found the breakdown and the items beat one that found only a total."""
        d = ex.data
        return sum(v is not None for v in (d.vendor, d.invoice_number, d.date, d.subtotal, d.tax, d.total)) + (1 if d.items else 0)

    @staticmethod
    def _score(out: PipelineOutput) -> tuple[int, int, int, float]:
        """Lower is better: error-severity issues, then key-field problems, then (negated) number of fields found, then (negated) confidence."""
        ex = out.extraction
        return (sum(1 for i in ex.issues if i.severity == "error"), len(DocumentPipeline._gate_problems(ex)), -DocumentPipeline._filled(ex), -ex.confidence)

    def _wants_retry(self, first: PipelineOutput, pages: list[Image.Image | None]) -> bool:
        plan = self.retry
        if plan is None or not plan.ops or len([p for p in pages if p is not None]) > plan.max_pages:
            return False
        ex = first.extraction
        if any(i.code in _NO_RETRY_CODES for i in ex.issues):
            return False
        if any(i.severity == "error" for i in ex.issues) or ex.confidence < plan.below_confidence:
            return True
        return plan.on_key_fields and bool(self._gate_problems(ex))

    def run(self, pages: list[Image.Image | None]) -> PipelineOutput:
        first = self._run_once(pages, self.ocr)
        if not self._wants_retry(first, pages):
            return first
        info: dict = {"attempted": True, "used": False, "ops": list(self.retries[0].ops), "psm": self.retries[0].psm,
                      "trigger": self._trigger(first), "first": {"confidence": first.extraction.confidence, "errors": self._score(first)[0],
                                                                   "gate": self._gate_problems(first.extraction)}, "passes": []}
        best, ocr_ms, ext_ms = first, first.ocr_ms, first.extraction_ms
        for plan in self.retries:
            if not plan.ops:
                continue
            if not self._needs_more(best):
                break
            try:
                retried_pages = [apply_ops(p, plan.ops) if p is not None else None for p in pages]
                second = self._run_once(retried_pages, plan.ocr)
            except Exception as exc:        # a failing extra pass never makes a job worse than its first result
                log.warning("ocr retry pass failed; keeping best result so far", extra={"error_class": type(exc).__name__})
                m.OCR_RETRIES.labels(outcome="error").inc()
                info["passes"].append({"ops": list(plan.ops), "psm": plan.psm, "error": type(exc).__name__[:40]})
                continue
            ocr_ms += second.ocr_ms; ext_ms += second.extraction_ms
            better = self._score(second) < self._score(best)
            info["passes"].append({"ops": list(plan.ops), "psm": plan.psm, "confidence": second.extraction.confidence,
                                   "errors": self._score(second)[0], "gate": self._gate_problems(second.extraction), "used": better})
            m.OCR_RETRIES.labels(outcome="used" if better else "not_better").inc()
            if better:
                best = second
        won = next((p for p in reversed(info["passes"]) if p.get("used")), None)
        info["used"] = won is not None
        if won is not None:                                          # keep the legacy single-pass keys for the winning pass
            info["ops"], info["psm"] = won["ops"], won["psm"]
            info["second"] = {"confidence": won["confidence"], "errors": won["errors"]}
        elif info["passes"] and "confidence" in info["passes"][-1]:
            info["second"] = {"confidence": info["passes"][-1]["confidence"], "errors": info["passes"][-1]["errors"]}
        elif info["passes"] and info["passes"][-1].get("error") and len(info["passes"]) == 1:
            info["error"] = info["passes"][-1]["error"]
        best.ocr_ms, best.extraction_ms = ocr_ms, ext_ms
        best.retry = info
        return best

    def _trigger(self, out: PipelineOutput) -> str:
        ex = out.extraction
        if any(i.severity == "error" for i in ex.issues):
            return "error_issue"
        if self.retry is not None and ex.confidence < self.retry.below_confidence:
            return "low_confidence"
        return "key_field"

    def _needs_more(self, out: PipelineOutput) -> bool:
        """Another variant is only worth running while the best result so far still has errors, key-field problems, low confidence or is
        still flagged for review. A variant never replaces a result unless it scores better, so the extra pass costs time, not accuracy."""
        ex = out.extraction
        plan = self.retry
        return (any(i.severity == "error" for i in ex.issues) or bool(plan and plan.on_key_fields and self._gate_problems(ex))
                or ex.confidence < (plan.below_confidence if plan else 0.5) or ex.requires_review)

    def _pick_extraction(self, ocr: OCRResult, pages, used: Extractor, out: ExtractionOutput, fallback_from: str | None):
        """Run alternate extractors on the SAME OCR result when the primary left a key field missing or suspicious. The candidate with
        fewer errors, then fewer key-field problems, then higher confidence wins; the primary wins ties. Returns (extractor, output, provenance)."""
        problems = self._gate_problems(out)
        if not self.alternates or not problems or fallback_from is not None:
            return used, out, None
        prov: list[dict] = [{"extractor": f"{used.name}@{used.version}", "errors": sum(1 for i in out.issues if i.severity == "error"),
                             "gate": problems, "confidence": out.confidence, "chosen": True}]
        best_key = (prov[0]["errors"], len(problems), -self._filled(out), -out.confidence)
        best_ex, best_out = used, out
        for alt in self.alternates:
            try:
                with tracing.span("job.extract_alternate", extractor=alt.name, model_version=alt.version):
                    _, alt_out = self._extract(alt, ocr, pages)
            except Exception as exc:     # an alternate that crashes is skipped, never fatal
                log.warning("alternate extractor failed", extra={"extractor": f"{alt.name}@{alt.version}", "error_class": type(exc).__name__})
                m.EXTRACT_STRATEGY.labels(outcome="error").inc()
                prov.append({"extractor": f"{alt.name}@{alt.version}", "error": type(exc).__name__[:40], "chosen": False})
                continue
            g = self._gate_problems(alt_out)
            key = (sum(1 for i in alt_out.issues if i.severity == "error"), len(g), -self._filled(alt_out), -alt_out.confidence)
            prov.append({"extractor": f"{alt.name}@{alt.version}", "errors": key[0], "gate": g, "confidence": alt_out.confidence, "chosen": False})
            if key < best_key:
                best_key, best_ex, best_out = key, alt, alt_out
        for p in prov:
            p["chosen"] = p["extractor"] == f"{best_ex.name}@{best_ex.version}"
        m.EXTRACT_STRATEGY.labels(outcome="used_alternate" if best_ex is not used else "kept_primary").inc()
        return best_ex, best_out, prov

    def _run_once(self, pages: list[Image.Image | None], ocr_provider: OCRProvider) -> PipelineOutput:
        t0 = time.perf_counter()
        with tracing.span("job.ocr", provider=ocr_provider.name, model_version=ocr_provider.version):
            ocr = self._recognize(pages, ocr_provider)
        t1 = time.perf_counter()
        failed = ocr.failed_pages
        if failed and self.fail_on_partial:
            raise OCRPermanentError("One or more pages could not be read")

        used, fallback_from = self.extractor, None
        with tracing.span("job.extract", extractor=self.extractor.name, model_version=self.extractor.version):
            try:
                _, out = self._extract(self.extractor, ocr, pages)
            except Exception as exc:
                if self.fallback is None or self.fallback is self.extractor:
                    if isinstance(exc, ExtractionError):
                        raise
                    raise ExtractionError("extraction failed") from exc
                m.EXTRACT_FALLBACKS.labels(reason=type(exc).__name__[:40]).inc()
                log.warning("extractor failed; using fallback",
                            extra={"extractor": f"{self.extractor.name}@{self.extractor.version}", "error_class": type(exc).__name__})
                fallback_from = f"{self.extractor.name}@{self.extractor.version}"
                used = self.fallback
                try:
                    _, out = self._extract(self.fallback, ocr, pages)
                except Exception as exc2:
                    raise ExtractionError("extraction failed") from exc2
        used, out, strategies = self._pick_extraction(ocr, pages, used, out, fallback_from)
        script_issue = unsupported_script_issue(ocr)
        if script_issue is not None:
            out.issues.append(script_issue)
            out.requires_review = True
        if failed:
            out.issues.append(ValidationIssue(code="page_ocr_failed", field=None, severity="error",
                                              message=f"Page(s) {', '.join(map(str, failed))} could not be read; result is incomplete"))
            out.requires_review = True
        t2 = time.perf_counter()
        po = PipelineOutput(ocr, out, int((t1 - t0) * 1000), int((t2 - t1) * 1000), used, fallback_from, failed)
        po.strategies = strategies
        return po
