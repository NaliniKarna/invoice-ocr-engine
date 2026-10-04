# Layout-aware extraction, key-field gate and multi-pass OCR retry

Status: implemented, tested on synthetic layouts only, **not validated on real invoices**. Off by default.

> **All benchmark numbers in this document come from SYNTHETIC invoices drawn by the same author as the extractor.
> They rank strategies on those layouts. They are NOT real-world accuracy and must not be quoted as such.**

## 1. Why this exists

The Olive invoice test showed that fixing one invoice at a time does not scale. Two different problems were mixed together:

1. **OCR / image quality.** Small or blurry images lose digits and words. The retry pass with upscaling addresses this.
2. **Layout understanding.** Even with perfect OCR the old `layout-rules` extractor failed. It had fixed label and column word lists,
   could not read `$1,234.56` as a number, took the "tallest header text" as vendor, and had one strategy only.

Under perfect word boxes (oracle OCR) `layout-rules` got all critical fields right on 0% of the synthetic documents. So most of the
failure was extraction, not OCR.

## 2. What was added (all additive)

| Piece | File | Purpose |
|---|---|---|
| Label lexicon | `app/extraction/lexicon.py` | Maps printed labels (Tax, Taxes, VAT (15%), IVA, Total Due, Net total ...) to roles with weights. Word list plus stemming and light fuzzy matching. Ambiguous labels keep several roles. |
| `layout-kv` extractor | `app/extraction/layout_kv.py` | Merges words into phrases by gap size; pairs labels with values to the right, below, or after a colon; finds tables from content (the qty x price = amount column triple); resolves ambiguous totals by arithmetic (subtotal - discount + tax + charges = total). Secondary fields (addresses, phone, email, tax IDs, customer, language) still come from `layout-rules`. |
| Required-field validation | `app/extraction/validation.py` | `missing_vendor`, `missing_invoice_number`, `missing_date`, `suspicious_vendor`, `suspicious_invoice_number`, `missing_tax`, `missing_subtotal`, `items_without_amounts`. Warning severity. Only active when `ValidationConfig.required_fields` is true (the worker sets it). |
| Key-field gate | `app/extraction/pipeline.py` | `GATE_CODES` issues trigger an alternate extractor and/or another OCR pass even when overall confidence is high. |
| Alternate extractors | `DocumentPipeline(alternates=...)` | Run on the same OCR result. Winner: fewer errors, then fewer gate problems, then more fields found, then higher confidence. Primary wins ties. Provenance in `meta.extraction_strategies`. |
| Multi-variant OCR retry | `RetryPlan` list, `retry_passes` | Variants run in order until the result has no error, gate problem, low confidence or review flag. A variant replaces the result only if it scores strictly better. Provenance in `meta.ocr_retry.passes`. |
| Date format fix | `app/extraction/normalization.py` | `24-Dec-2025` style dates now parse. |

Unchanged: public API, feedback system, evaluation framework (`app/ml`), model registry, tenant isolation, ML governance.
The regex baseline and `layout-rules` remain available and remain the fallback if an extractor crashes.

## 3. Settings

All new settings are off or neutral by default. Defaults for `reference_extractor` (`rule-based-baseline`), `extractor_alternates` (empty) and
`retry_passes` (empty) are unchanged.

| Variable | Default | Meaning |
|---|---|---|
| `OCR_REFERENCE_EXTRACTOR` | `rule-based-baseline` | now also accepts `layout-kv` |
| `OCR_EXTRACTOR_ALTERNATES` | empty | comma list of `rule-based-baseline`, `layout-rules`, `layout-kv` |
| `OCR_RETRY_PASSES` | empty | `ops@psm;ops@psm`. Empty = the single pair from `OCR_RETRY_PREPROCESS_OPS` / `OCR_RETRY_TESSERACT_PSM` |
| `OCR_RETRY_ON_KEY_FIELDS` | `true` | retry also on a missing or suspicious key field. Only matters when a retry is configured |
| `OCR_VALIDATE_REQUIRED_FIELDS` | `true` | report required-field issues. **This changes output for existing deployments**: missing vendor / invoice number / date adds a warning and lowers confidence by 5% each |

Note: `docker-compose.yml` already sets `OCR_RETRY_PREPROCESS_OPS=upscale,adaptive` from the earlier receipt work. That is a separate, earlier
decision and was not changed.

## 4. Controlled real-invoice trial: exact configuration

Add to the worker environment (docker compose `environment:` or `.env`), then `docker compose up -d --build`:

```
OCR_REFERENCE_EXTRACTOR=layout-kv
OCR_EXTRACTOR_ALTERNATES=
OCR_RETRY_PASSES=upscale@3;upscale,adaptive@6
OCR_RETRY_ON_KEY_FIELDS=true
OCR_VALIDATE_REQUIRED_FIELDS=true
```

In docker-compose.yml, quote the semicolon value:

```yaml
    OCR_REFERENCE_EXTRACTOR: layout-kv
    OCR_EXTRACTOR_ALTERNATES: ""
    OCR_RETRY_PASSES: "upscale@3;upscale,adaptive@6"
```

When `OCR_RETRY_PASSES` is set it replaces `OCR_RETRY_PREPROCESS_OPS` / `OCR_RETRY_TESSERACT_PSM`. The regex baseline stays the crash fallback.
Run the trial only for a chosen test tenant or a separate stack, not as the global default.

What to check per job: `result.meta.ocr_retry` (trigger, passes, which pass won), `meta.extraction_strategies` (empty here, no alternates),
issues with codes in `GATE_CODES`, and `preprocessing_version`. Compare against the baseline configuration on the SAME documents through the
existing evaluation framework (`app/ml/evaluation.py`, paired bootstrap), per field.

## 5. Synthetic benchmark

Harness: `benchmarks/layouts.py` (18 HTML layouts rendered with Chromium; exact word boxes from the DOM) and `benchmarks/layout_bench.py`.
3 documents per layout, 54 documents. Modes: `oracle` (perfect OCR, isolates extraction), `real` (Tesseract defaults on the rendered image),
`lowres` (560 px wide JPEG, quality 35).

Splits: layouts 1-6 are **dev** (extractor work looked at their failures). Layouts 7-12 are **held-out**, but I looked at some of their failures while
diagnosing, so they are not clean. Layouts 13-18 are **round 2**: written after the extractor was frozen and run once. Two generic bugs found there were
fixed afterwards (summed CGST+SGST value; unknown first header word dropped), so round 2 is no longer untouched either.

Metric: share of documents where vendor, invoice number, date, subtotal, tax and total are all right (money within 0.005). n = 18 documents per cell,
so differences of a few points are noise.

| Extractor | Dev oracle | Dev real | Held-out oracle | Held-out real | Round 2 oracle | Round 2 real |
|---|---|---|---|---|---|---|
| `regex-baseline` | 0.06 | 0.00 | 0.17 | 0.00 | 0.17 | 0.06 |
| `layout-rules` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| `layout-kv` | 1.00 | 0.61 | 1.00 | 0.89 | 0.67 | 0.67 |

Round 2 `layout-kv` before the two bug fixes: 0.50 (file `benchmarks/results/layouts_round2_frozen_prefix.json`).
Low-res (`lowres`), `layout-kv` without retry: 0.00 on all three splits.

With two retry variants (`pipe:kv+alts+retry`, alternates included in that run):

| Mode | Dev | Held-out | Round 2 |
|---|---|---|---|
| real | 0.67 | 0.94 | 0.67 |
| lowres | 0.28 | 0.61 | 0.33 |

Alternates: with `layout-rules` as primary and `layout-kv` as alternate, oracle held-out went 0.00 to 1.00 and real held-out 0.00 to 0.89, so the
mechanism works as a rescue for a weak primary. With `layout-kv` as primary and alternates on, round 2 did **not** improve (oracle invoice number
0.83 to 0.72, item F1 0.83 to 0.67), so the trial config disables alternates.

Raw results: `benchmarks/results/final_a.json`, `final_r2.json`, `final_retry.json`, `final_r2_retry.json`. A later re-run of the pipeline rows after a small
scoring change (completeness term, continue variants while review is needed) was stopped before finishing; those rows reflect the earlier scoring.
The unit tests cover the changed behaviour.

### Real images (anecdotal, n = 3, not a benchmark)

Config: `layout-kv`, alternates on, retry variants `upscale@3;upscale,adaptive@6`.

| Image | Result |
|---|---|
| Wine Town receipt photo | vendor, bill no. 25, subtotal 368.00, tax 18.40, total 1384.00, 3 items. Same as before. |
| Olive screenshot, 529 px | vendor `Olive`, invoice 2205912, tax 22.05 right. Year read as 2025 (printed 23) and total 97.50 (printed 97.25): OCR errors. Old setup returned a garbage vendor and number. |
| Olive replica at 1x (rendered by me) | invoice, date, total right; vendor `View` is wrong and the gate did not flag it. |

## 6. Limitations

- **Real invoice accuracy is not measured.** Every number above is synthetic.
- **Wrong-but-plausible values bypass the gate.** The gate sees missing or implausible fields and failed arithmetic. A wrong vendor such as `View`, or a
  plausible wrong date, raises nothing. Overall confidence is also a mean of four fields and can stay high.
- **Low-resolution OCR remains a major failure mode.** `layout-kv` scores 0.00 on 560 px JPEGs without retry and 0.28 to 0.61 with it. Digits lost
  by OCR (year 2023 read as 2025, 97.25 read as 97.50) are not detectable by extraction.
- **Alternate-extractor arbitration does not currently improve round 2** when `layout-kv` is the primary, and can lower it. Keep alternates empty.
- **Retry latency and cost are not measured.** Each extra variant is a full OCR pass. Variants keep running while a result is still flagged for review,
  so review-flagged documents can pay for several passes. Measure p50/p95 job time and CPU before any wider use.
- **Multilingual labels are not covered.** The lexicon is mostly English with a few Spanish, German and French words. The German round-2 layout
  (Rechnung, Nettobetrag, Gesamtbetrag) fails by design and returns missing fields rather than wrong ones. Devanagari remains unsupported.
- **Handwriting is not covered.**
- Other known gaps on synthetic layouts: a bare `No.` as the invoice-number label; vendor printed only at the bottom or inside a logo image;
  vendor in white text on a dark band can be missed by OCR; `Total` labels with unusual wording; multi-page tables with no repeated header
  rely on the baseline path.
- The pretrained document-AI model comparison was **not run**: HuggingFace is unreachable from the build sandbox and `torch` is not installed.
  The `Extractor` interface is ready for it, but nothing about its quality is known.
- Lexicon words were added after seeing dev failures (for example `imponible`). That is allowed for dev, and is a form of tuning to my own layouts.

## 7. Architecture notes

Deterministic and unchanged: normalisation, validation arithmetic, the retry decision, tenant isolation, security. Machine learning should enter
as one more candidate generator behind the `Extractor` interface (a pretrained layout model first, a fine-tuned field model only after labelled
real data exists). No model was trained.

Real labelled data needed: 300-500 consented real documents, at least 30 per layout family, field labels with page and box, a mix of scans, phone
photos, native PDFs and screenshots, and a frozen test set split by vendor. The feedback table is a cheap source.

Metrics to use on real data: per-field exact match (money within 0.01), line-item F1, share of documents with all critical fields right,
auto-accept rate at a fixed error rate, review rate, OCR-loss gap (oracle OCR versus real OCR), and a paired bootstrap on per-document scores.

## 8. How to reproduce

```
pip install playwright   # Chromium is needed
PYTHONPATH=. python -m benchmarks.layout_bench --strategies regex-baseline,layout-rules,layout-kv --modes oracle,real --json out.json
PYTHONPATH=. python -m benchmarks.layout_bench --round2 --strategies layout-kv --modes oracle
```

A full run with real OCR and retry takes about 20 to 30 minutes on 2 CPUs.

## 9. Recommendation

The next phase is **real-invoice evaluation**, not more synthetic-layout feature development. The synthetic layouts are saturated on the dev
split and cannot show real-world behaviour. Run the controlled trial in section 4 on 30 to 50 hand-labelled real invoices (diverse vendors,
phone photos and screenshots included), score per field with the existing evaluation framework, measure retry latency, and decide from that evidence
whether to adopt `layout-kv`, add vocabulary, or evaluate a pretrained document-AI model.
