# OCR, extraction, evaluation and ML lifecycle (v0.3)

This document describes what exists, what it does not do, and the rules the pipeline enforces. Measured numbers live in
[`BENCHMARK.md`](BENCHMARK.md). There is **no representative labelled dataset** for this system. Every quality statement
below is either a property of the code (tested) or explicitly marked as unmeasured.

## 1. Pipeline

```
bytes -> sandboxed render (pdfium / PIL, optional preprocessing ops) -> OCR (page by page) -> layout -> extraction
      -> normalisation (with provenance) -> validation -> heuristic confidence -> persist (result + internal evidence)
```

| Stage | Module | Notes |
|---|---|---|
| Render / preprocess | `app/ocr/preprocess.py`, `preprocess_ops.py`, `render_child.py` | Runs in the rlimit-bounded child. Ops (`autocontrast`, `median3`, `sharpen`, `otsu`, `deskew`) are **off by default**; enabled with `OCR_PREPROCESS_OPS`. The active list is part of `preprocessing_version` (for example `1+ops1:autocontrast,deskew`; `1` when no ops are enabled). A page that cannot be rendered becomes a failed page. |
| OCR | `app/ocr/base.py`, `providers.py` | `OCRProvider.recognize_page` is implemented per engine; the shared `recognize` isolates permanent page errors and re-raises transient ones. Engines: Tesseract (default), RapidOCR (optional). Word boxes are kept when the engine provides them. |
| Layout | `app/layout/` | `LayoutAnalyzer` interface; `HeuristicLayoutAnalyzer` groups OCR boxes into rows, finds a table by header keywords, assigns columns by x position, detects a totals region, continues a table across pages. A learned layout model can implement the same interface. |
| Extraction | `app/extraction/` | `RuleBasedExtractor` (regex, **the reference baseline, unchanged in behaviour**) and `LayoutRulesExtractor`. Extractors return strings plus evidence; they never produce final typed values. |
| Normalisation | `app/extraction/normalization.py` | Every value keeps `raw`, `value`, `transforms`, `risky`, `ambiguous`. Nothing is silently rounded or repaired. |
| Validation | `app/extraction/validation.py` | Business rules with configurable tolerances (section 4). |
| Confidence | `app/extraction/postprocess.py` | Heuristic only (section 5). |

**Separation.** OCR knows nothing about invoices. Extractors consume `OCRResult` (lines, words, boxes, per-page status).
`DocumentPipeline` runs them, validates the extractor output against the canonical schema, and falls back to the reference
extractor when an extractor crashes or returns invalid output. The result records which extractor really produced it.

## 2. Canonical schema (`extraction_schema_version`)

`InvoiceData` v2 is **additive** over v1: every v1 field keeps its name and meaning (`vendor` is the supplier name, `total`
is the grand total). v2 adds document type and number, due date, `date_bs`, language, supplier address / phone / email /
tax id, customer block, discount, taxable amount, service and other charges, amount paid and due, and per-line unit,
discount and tax. The stored result carries `schema_version`; results written as "1" still validate as v2 and need no
migration. The public response omits fields that are null, so a v1 client sees what it saw before, plus new keys when they
have values. Money is `Decimal`, stored as strings in JSONB.

## 2a. Unsupported script

Devanagari OCR is **unsupported**. If an engine emits Devanagari characters (at least 10, at least 10% of letters) the result gets the error-level issue `unsupported_script` and `requires_review=true`. The default engine is English Tesseract, which does not emit Devanagari; it emits Latin-looking noise, and that case is not detected as a script. On one synthetic Nepali invoice rendered with a Devanagari font, the English engine produced wrong values (vendor, date, tax) with confidence 0.13 and `requires_review=true`, because validation and OCR confidence also fired. That is one sample, not a guarantee. Do not send Devanagari documents to this system without human review.

## 3. Evidence and provenance (internal)

Each extracted field has a `FieldEvidence`: raw text, normalised value, transforms, page, bounding box, source text, method,
raw OCR confidence, heuristic confidence. Line items carry the same. Evidence, per-page status, stage versions and fallback
information are stored in `ocr_results.meta` (migration 0003). **The public API never returns evidence** (it contains OCR
text). Erasure clears it with the result (`purge_process`). Logs never contain evidence or OCR text.

## 4. Validation tolerances

`allowed difference = max(abs_tolerance, rel_tolerance x larger reference amount)`. Defaults: 0.02 currency units and 0.05%
(`OCR_VALIDATION_TOLERANCE_ABS`, `OCR_VALIDATION_TOLERANCE_REL`). The absolute part absorbs two rounding steps of 0.01; the
relative part absorbs per-line rounding on long invoices. These defaults are engineering judgement, not fitted to data.
Two accounting variants are accepted for the totals equation (discount applied before or already inside the subtotal), and a
line amount may include or exclude line tax. A check runs only when the fields it needs are present. Validation reports
issues; it never changes a value. `error` severity forces human review.

Dates: Gregorian and Bikram Sambat. BS is detected by an explicit marker (BS, वि.सं., Miti, मिति) or a year above the
current Gregorian year and up to 2099, converted with `nepali-datetime`, and the original is kept in `date_bs`. A BS date
that cannot be converted is kept verbatim and never guessed. Ambiguous day/month dates follow `OCR_ASSUME_DAY_FIRST_DATES` and
are flagged. Letter/digit OCR repair (O->0, l->1) applies only to tokens that are otherwise numeric and is marked risky.

## 5. Confidence

Three different things, never mixed:

* **raw**: engine-reported confidence of the source text (`FieldEvidence.ocr_confidence`);
* **heuristic**: rule-based score (`confidence_method = "heuristic-v2"`), the value in `confidence`; `0.8^errors x 0.95^warnings`
  over the key fields, risky values x0.6. It is **not a probability**;
* **calibrated**: produced only by an isotonic calibrator fitted on held-out labelled data (`app/ml/calibration.py`,
  minimum 500 labelled documents, reports ECE/Brier before and after on a holdout). No calibrator exists, so
  `confidence_calibrated` is `false` everywhere. `ece_provisional` in evaluation output is an ECE of the heuristic score
  against correctness on the evaluated set; on synthetic data it says nothing about production.

## 6. Data lifecycle

```
raw document -> OCR output -> machine result (immutable) -> client correction (feedback) -> validated correction
   -> proposed example -> [quality gates] -> reviewed/approved example -> dataset snapshot (train/val | test)
   -> candidate model -> immutable evaluation -> approved -> production
```

These are different objects in different tables. A correction is evidence, not ground truth: it becomes a *proposed* example,
and only an operator review (`POST /internal/v1/examples/{id}/review`) can approve it. A machine result alone is never an
example (`no_corrections`). `ocr_results.data` is never modified.

**Consent.** `clients.training_policy`: `none` (default), `tenant_only` (data may only be used for a dataset scoped to that
tenant), `pooled` (may join cross-tenant datasets; set by an operator under contract). `training_opt_in` remains as a mirror of
`pooled`. A dataset is built for one scope (`pooled` or `tenant:<id>`); tenants that are not `pooled` are excluded from pooled
scope by the query, and a tenant-scoped dataset holds only that tenant's examples. Examples carry `policy_at_creation` for audit.

**Quality gates** (`app/ml/quality.py`; flag, never repair): schema mismatch, malformed annotation, missing required fields,
impossible dates and amounts, inconsistent totals and line items, failed pages, unusable OCR, contradictory corrections,
duplicates. Blocking issues prevent approval.

**Splits** (`app/ml/splits.py`): deterministic by `(seed, group)`, where a group is tenant + normalised supplier (template
family). A group is never split across train/val/test; adding documents never moves an existing group. An evaluation dataset
contains only test-split groups that are not in any active training dataset of the scope, and later training datasets exclude
evaluation groups. Exact duplicates are kept once. Small evaluation sets are refused or flagged (fewer than 5 groups is reported
as not statistically meaningful).

**Anti-poisoning.** Per-tenant daily contribution limit; a per-tenant share cap in pooled datasets (when 3 or more tenants
contribute); anomaly flags (most key fields changed in one submission, total changed by more than 100x, bursts from one API key);
nothing is auto-approved, so every flag ends in a human decision. These are heuristics, not a proof against a determined
adversary.

**Privacy and deletion.** Dataset artifacts hold ground-truth fields and pseudonymous tenant references only (no OCR text, file
names or keys). Deleting a process (API or retention) removes derived examples unless the tenant is `pooled` (same rule that
already applied to feedback). Setting the policy to `none` erases all of the tenant's examples; lowering from `pooled` flags
pooled datasets that contain the tenant. **A flagged dataset** (`flagged_withdrawal`) cannot be used for training and must be
rebuilt. Already-trained models are not retrained automatically: removing a tenant's influence from a trained model requires
retraining on a rebuilt dataset, which is an operator action. This limitation is real and unsolved here. What exists is identification: `GET /internal/v1/models` returns, for every model, `dataset_id`, `parent_id`, `affected_by_withdrawal` and `flagged_datasets` (a model is affected when its own dataset, or the dataset of any ancestor in its lineage, is flagged), so an operator can list exactly which models must be retrained on a rebuilt dataset.

## 7. Registry, gates, rollback

States: `candidate -> evaluated -> approved -> production -> retired` (`rejected` from any pre-production state). One
`production` model per kind (partial unique index). `rollback` re-activates a retired model that was once in production
(A -> B -> A tested). Every transition is audited.

`approve_with_gates` compares the candidate's latest test-split run with the **production model's run on the same dataset**
(or an explicit reference run, for example the reference extractor). Gates (all configurable; `PromotionGates`):
minimum samples (default 200), synthetic evidence never approves (default), floor on the primary metric, strict improvement over
the reference, no regression on guarded metrics (`field_f1`, `critical_field_f1`, `total_exact`, `date_exact`, `line_item_f1`),
latency ratio, same evaluation dataset, schema match, and a reference must exist. Evaluation runs are **immutable** (ORM guard
plus a PostgreSQL trigger). Feedback cannot reach production: it needs examples, datasets, training, evaluation, gates and an
explicit promote call.

If no model is in production, or a production model cannot be loaded, workers use the reference extractor.

## 8. Loading models safely (`app/ml/runtime.py`, `artifacts.py`)

* Loaded once per worker process (warm-up at process start, then a registry poll at most every 30 s; never per document).
* An artifact must have an allowed data extension (`.json`, `.safetensors`, `.onnx`), a matching SHA-256 and a valid HMAC
  signature from `OCR_ML_MODEL_SIGNING_KEY`; pickle-family files are refused by extension and by magic bytes. The artifact location must be under `models/` and pass the traversal-proof key check (`app/storage/base.validate_key`); this is verified before any object is read, so a registry row cannot make a worker read a tenant document or a dataset. With no signing
  key, artifact-backed models are never loaded.
* Schema compatibility is checked; an unknown model type, a bad artifact, a missing object or a load error selects the reference
  extractor, increments `ml_model_load_errors_total{reason}`, and is sticky for that model id (no retry storm).
* A registry outage keeps the currently loaded model.

## 9. Training

`app/ml/training.py` wraps a `Trainer` (interface only): it verifies the dataset checksum, refuses flagged datasets, datasets
containing the test split and datasets below the readiness policy (defaults: 1000 training examples, 100 groups, 100 validation
examples, 50 labelled examples per critical field), then signs the artifact and registers a **candidate** with reproducibility
metadata (seed, hyperparameters, dataset id and checksum, trainer, Python and platform). **No trainer ships**, because training
on the data that exists (none) would be fabrication. The custom-model decision is in `BENCHMARK.md`.

## 10. External providers

Denied by default and not implemented. `app/ml/external.py` is the single gate a future integration must pass: operator flag,
allow-list, and per-tenant consent (`clients.external_processing`). No document content leaves the deployment.

## 11. Metrics (low-cardinality labels only)

`ocr_engine_duration_seconds`, `extraction_duration_seconds`, `extraction_fallbacks_total{reason}`,
`extraction_validation_issues_total{code,severity}`, `extraction_low_confidence_total`, `ocr_page_failures_total{provider,code}`,
`ocr_document_pages`, `ml_examples_total{outcome}`, `ml_datasets_built_total{kind,scope_type}`, `ml_evaluation_runs_total`,
`ml_model_load_seconds`, `ml_model_load_errors_total{reason}`, `ml_reference_fallback_total{reason}`. Tenant ids, process ids,
model ids and field values are never labels.

## 12. Operator API (`/internal/v1`, admin token)

`PUT /clients/{id}/training-policy`, `POST/GET /examples`, `POST /examples/{id}/review`, `POST /datasets`, `GET /datasets/{id}`,
`GET /models`, `GET /models/{id}/evaluations`, `POST /models/{id}/approve|promote|rollback|retire`. Tenant API keys cannot call
these (tested). Responses never contain ground-truth values or OCR text. Public API changes are additive only.
