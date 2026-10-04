"""Data-quality gates for labelled examples. Pure functions: no I/O, no mutation of the input.

The gates FLAG, they never repair. A flagged example is `needs_review`; ground truth is never "fixed" silently.
Severity `error` blocks approval. Severity `warning` is recorded in the quality report and shown to the reviewer.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from app.extraction.schema import SUPPORTED_SCHEMA_VERSIONS, InvoiceData
from app.extraction.validation import ValidationConfig, validate

QUALITY_VERSION = "1"


@dataclass(frozen=True)
class QualityConfig:
    required_fields: tuple[str, ...] = ("vendor", "total", "date")
    min_ocr_text_chars: int = 20          # below this the OCR output is treated as unusable
    max_failed_page_fraction: float = 0.0  # any failed page blocks approval by default: ground truth must match the pages
    validation: ValidationConfig = ValidationConfig()


def content_hash(truth: dict) -> str:
    return hashlib.sha256(json.dumps(truth, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def group_key_for(truth: dict, *, tenant_id: str, batch_id: str | None = None) -> str:
    """Family key used to keep near-duplicate documents on one side of a split: tenant + normalised supplier name.
    Invoices from the same supplier share a template, so they must not straddle train and evaluation."""
    vendor = " ".join((truth.get("vendor") or "unknown").lower().split())
    return hashlib.sha256(f"{tenant_id}|{vendor}".encode()).hexdigest()[:32]


def check_example(truth: dict, *, schema_version: str, ocr_chars: int | None = None, pages_total: int | None = None,
                  pages_failed: int = 0, conflicting_fields: list[str] | None = None,
                  duplicate_of: str | None = None, cfg: QualityConfig = QualityConfig(), today: date | None = None) -> list[dict]:
    issues: list[dict] = []

    def add(code: str, severity: str, field: str | None = None, message: str = "") -> None:
        issues.append({"code": code, "severity": severity, "field": field, "message": message})

    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        add("schema_mismatch", "error", None, f"schema {schema_version} is not supported")
    try:
        data = InvoiceData.model_validate(truth)
    except ValidationError as exc:
        for e in exc.errors()[:10]:
            add("malformed_annotation", "error", ".".join(str(x) for x in e["loc"]), e["type"])
        return issues
    for f in cfg.required_fields:
        if getattr(data, f) in (None, ""):
            add("missing_required_field", "error", f, f"{f} is required for approval")
    # validation() already covers: impossible dates/amounts, negative totals, totals & line-item consistency, currency format.
    for i in validate(data, today=today, cfg=cfg.validation):
        # A failed arithmetic check on GROUND TRUTH means the annotation or the document is questionable: flag, do not fix.
        add(f"validation:{i.code}", "error" if i.severity == "error" else "warning", i.field, i.message)
    if ocr_chars is not None and ocr_chars < cfg.min_ocr_text_chars:
        add("unusable_ocr", "error", None, "OCR produced almost no text")
    if pages_total and pages_failed and pages_failed / pages_total > cfg.max_failed_page_fraction:
        add("pages_failed", "error", None, f"{pages_failed}/{pages_total} pages failed OCR or rendering")
    if conflicting_fields:
        for f in conflicting_fields:
            add("contradictory_annotation", "error", f, "different corrections were submitted for this field")
    if duplicate_of:
        add("duplicate_example", "error", None, f"same ground truth as {duplicate_of}")
    return issues


def blocks_approval(issues: list[dict]) -> bool:
    return any(i["severity"] == "error" for i in issues)
