# OCR retry pass and receipt handling (v0.3.1)

Status words follow the project vocabulary: implemented, tested, benchmarked, verified, unverified.

## Why

A phone photo of a printed till receipt (Wine Town cash memo, 423x694 px, blue tint, creased) returned: vendor
`Chandani Chowk ,Bavdhan Pune nan` (the address line; `nan` was OCR noise for the postcode), no total, no tax, no date, no
invoice number, no currency, no items, confidence 0.083, `requires_review: true`. The review flag was correct; the
extraction was not.

Diagnosis on the stored OCR text and on re-runs with different image treatment:

* The image is small. Tesseract needs letters roughly 30 px high. Upscaling to a 2000 px long edge and using a local
  (adaptive) threshold recovered every line; a global threshold or no preprocessing lost the item lines, the date and
  half of the totals block.
* The extractor then failed on conventions that synthetic invoices never used: `Bill No. : 25` (short number), a header row
  split by OCR (`part iculars`), a second item section after a section total, `CGST` + `SGST` instead of one tax line,
  a bare `Total : 1384` printed after `Food Total` and `Liquor Total`, and no currency symbol (GST markers only).

## What changed

| Area | Change |
|---|---|
| Preprocessing ops | New `upscale`, `flatten`, `adaptive` (numpy/PIL only, deterministic, bounded). Existing ops unchanged. |
| OCR | `OCR_TESSERACT_PSM` (default 3, unchanged behaviour). |
| Retry pass | `RetryPlan` in `DocumentPipeline`: if the first result has an error-severity issue or confidence below `OCR_RETRY_BELOW_CONFIDENCE` (0.5), run OCR again with `OCR_RETRY_PREPROCESS_OPS` and `OCR_RETRY_TESSERACT_PSM`; keep the second result only if it has fewer errors (ties: higher confidence). Not run for `unsupported_script` / `page_ocr_failed`, nor for documents over `OCR_RETRY_MAX_PAGES` (3). A failing retry never worsens a job. Provenance is stored in `meta.ocr_retry`; `preprocessing_version` records the ops of the winning pass. Metric `ocr_retry_passes_total{outcome=used|not_better|error}`. |
| Layout | Header words split by OCR are rejoined; a garbled first header column becomes the description; rows after a section total reopen the item table (strict: item rows only); long item names stay in the description column; stray separator rows no longer end the totals block. |
| Extraction | Short invoice numbers behind an explicit `No.`; vendor skips copy markers (`DUPLICATE`), titles and address lines; address skips titles/separators; CGST/SGST/IGST summed into `tax`; bare `Total` wins over `<section> Total`; several section totals without a grand total leave `total` empty (forces review) instead of guessing; currency `INR` inferred from GSTIN/CGST/SGST/IGST (confidence 0.6, method `inferred:gst_marker`); `€` followed by a percentage (OCR of `@2.5%`) is not a currency. |
| Validation | Whole-unit round-off: when the printed total has no fraction, a difference below 0.5 is accepted. Items + tax + charges may explain the total on multi-section bills (printed Sub Total covers one section only). |

Compose enables the retry (`OCR_RETRY_PREPROCESS_OPS=upscale,adaptive`, `OCR_RETRY_TESSERACT_PSM=6`). The code default is off.

## Evidence

* Real photo (1 image, the one that exposed the problem): all of vendor, address, bill number, date, currency, subtotal,
  tax, total (1384) and the 3 items extracted correctly through the real Tesseract 5.3 pipeline; no error issues; the
  result is still `requires_review` because the date `09/04/22` is ambiguous (day-first assumed) and OCR confidence is
  modest. Test: `tests/integration/test_receipt_photo.py` (skipped when the tesseract binary is missing).
  **n = 1.** This is a regression test, not an accuracy measurement.
* Synthetic set (24 documents, seed 7, same generator as `docs/BENCHMARK.md`), Tesseract + layout-rules, critical-field F1:
  default first pass 1.00; `upscale,adaptive` psm 3: 0.87; `upscale,adaptive` psm 6: 0.82; `upscale,flatten` psm 3: 0.99.
  Receipt-style settings therefore hurt clean documents, which is why they run only as a retry.
* With the retry enabled on the same 24 synthetic documents: the retry never ran, results identical (24/24 total, invoice
  number, vendor), median latency unchanged (1.5 s). Synthetic data only.
* Tests: 473 passed, 9 skipped (8 need PostgreSQL, 1 needs `opentelemetry.sdk`) on SQLite + fakeredis in the sandbox.

## Limits

* One real receipt. Nothing here measures accuracy on real invoices; collect 10-20 samples (consented) before trusting it.
* The retry triggers on error issues or low confidence. A wrong result with no error issue and confidence above 0.5 is not retried.
* A retry doubles OCR time for the documents that need it (up to the 60 s per-page limit twice, inside the 300 s job limit).
* Ambiguous `dd/mm/yy` dates stay flagged (`date_ambiguous`); GSTIN is not extracted as a tax id yet.
* Devanagari and handwriting remain unsupported.

## Update: several retry variants and the key-field trigger

`OCR_RETRY_PASSES="upscale@3;upscale,adaptive@6"` runs variants in order. The retry now also fires when a key field (vendor, invoice number,
date, total) is missing or suspicious, even with confidence above `OCR_RETRY_BELOW_CONFIDENCE` (switch: `OCR_RETRY_ON_KEY_FIELDS`). A variant
replaces the result only if it has fewer errors, then fewer key-field problems, then more fields found, then higher confidence. Later variants keep
running while the best result is still flagged for review. Latency and cost of this are not measured. Details, limits and the synthetic benchmark
(not real-world accuracy): `docs/LAYOUT_EXTRACTION.md`.
