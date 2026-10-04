# Benchmark report (v0.3.0)

**Read this first.** There is no representative labelled dataset of real invoices for this system. Every number below was
measured on a **synthetic** set produced by `benchmarks/synthetic.py` (seed 7, 72 documents, English text, DejaVu Sans, three
templates, six degradations). The templates are visible to the authors of the extractors, so the layout-rule extractor
scoring near the ceiling on it is expected and is **not** evidence about real invoices. These numbers show that the
harness works and how the engines behave on clean typeset English. They do not predict accuracy on real client documents
(real scans, camera photos, stamps, handwriting, Devanagari, unusual templates).

Raw results: `benchmarks/results/` (immutable JSON, see its README). Reproduce:
`PYTHONPATH=. python -m benchmarks.run --n 72 --seed 7 --out benchmarks/results` (about 25 minutes on 2 CPUs) and
`PYTHONPATH=. python -m benchmarks.resources --n 12`.

## 1. Baseline recorded before any change

| Item | Baseline (Prompt 2 code, v0.2.0) |
|---|---|
| OCR engine | Tesseract 5.3.4 (`eng`), line-level text with word boxes |
| Rendering | pdfium in a sandboxed child process, resolution capped (about 180 dpi, 2600 px longest side); images EXIF-rotated and size-bounded |
| Preprocessing | none beyond decode and bound |
| Extractor | `RuleBasedExtractor`, regex on line text, version `0.1.0`. Kept as the reference baseline; its behaviour is unchanged (it now also attaches evidence) |
| Confidence | heuristic, uncalibrated |
| Labelled real data | **none** |
| Baseline accuracy on real data | **unknown** (never measured) |

## 2. Measured

All figures: 72 synthetic documents, run 2 (`20260930T193419Z-e852ba16f3e3.json`), unless stated.

### 2.1 OCR candidates (text quality against the exact text the generator drew)

| Candidate | CER | WER | numeric char accuracy | date token accuracy | failures |
|---|---|---|---|---|---|
| tesseract, no ops (baseline) | 0.1037 | 0.1381 | 0.9364 | 1.000 | 0 |
| tesseract + autocontrast | 0.0998 | 0.1353 | 0.9396 | 1.000 | 0 |
| tesseract + deskew | 0.0998 | 0.1336 | 0.9386 | 1.000 | 0 |
| tesseract + median3,otsu | 0.0817 | 0.1012 | 0.9476 | 1.000 | 0 |
| tesseract + autocontrast,deskew | 0.1041 | 0.1402 | 0.9332 | 1.000 | 0 |
| rapidocr, no ops | 0.1719 | 0.3429 | 0.8861 | 1.000 | 0 |
| rapidocr + deskew | 0.1589 | 0.3355 | 0.8990 | 1.000 | 0 |

CER of 0.08 to 0.17 on clean typeset synthetic text is high. The cause was not analysed. Reading order differing from the ground-truth row text may contribute, so CER here may mix recognition errors with reading order. Use these values to compare configurations on the same set only; do not read them as engine accuracy.

### 2.2 Preprocessing

* `median3,otsu` lowered Tesseract CER from 0.1037 to 0.0817 on this set. For the extractor the paired bootstrap gave
  `inconclusive` (mean document-score change +0.012, 95% interval -0.0009 to +0.026).
* `autocontrast` and `deskew` made no meaningful difference to Tesseract. The synthetic rotation is only up to 1.6 degrees,
  which Tesseract tolerates. Deskew is therefore **not shown to help** here. It might help for larger skew; that is
  unmeasured.
* **Run 1 is superseded.** In run 1 (`20260930T190939Z-b84ce3b5e129.json`) `deskew` looked like a large gain
  (Tesseract CER 0.0578). That run used the first deskew implementation, which mis-rotated low-contrast pages by up to
  5 degrees. The implementation was fixed (guards for low contrast and ink, minimum gain) and tests were added. Run 2 is
  the valid measurement of the shipped code. Run 1 is kept as history and must not be quoted.
* No preprocessing op is enabled by default. Whether any op helps real documents is unmeasured.

### 2.3 Extraction (on replayed OCR output, through the production `DocumentPipeline`)

Tesseract, no preprocessing:

| Metric | `rule-based-baseline` | `layout-rules` |
|---|---|---|
| field micro F1 | 0.683 | 0.836 |
| critical-field F1 | 0.788 | 0.998 |
| total exact | 0.708 | 1.000 |
| line-item row F1 | 0.803 | 0.969 |
| line-item row accuracy (whole row correct) | 0.663 | 0.795 |
| documents with all critical fields correct | 0.236 | 0.986 |

Paired bootstrap on per-document scores (n = 72, 95%): `layout-rules` over `rule-based-baseline` is `b_better`,
mean difference +0.392 (interval +0.350 to +0.434). This is a large gap on documents whose templates the layout rules were
written against. It is not a claim about real invoices.

Line items are weaker than header fields: whole-row accuracy is 0.795 for the better extractor even on this easy set.

Other measured facts:
* Layout-rules on RapidOCR: field F1 0.823, critical F1 1.000. The reference extractor on RapidOCR: total exact 0.000, because RapidOCR returns split cells rather than lines (known limit of the line-based baseline).
* Tesseract beat RapidOCR for the layout-rules extractor (difference -0.022 for RapidOCR, interval -0.038 to -0.005, `a_better` for Tesseract).
* Per-slice (layout-rules, Tesseract): critical F1 is 1.0 on every degradation except noise (0.989); the reference extractor is lowest on blur (critical 0.698, F1 0.447).
* Fields that are not in the synthetic ground truth (language, supplier address and phone, customer address) have zero support, so their F1 of 0 is "not evaluated", not "wrong".
* Failures: 0 of 72 for every OCR candidate. Fallback rate: 0.

### 2.4 Latency and memory (isolated run, one engine per process)

File: `resources-20261001T021614Z-8531c631c7.json`. 2 CPUs, 8 GiB RAM, machine idle (load average 0.24 at start), 12 pages
(11 timed warm pages), synthetic images.

| Engine | warm page p50 | warm page p95 | peak RSS | model files | cold start | first page |
|---|---|---|---|---|---|---|
| Tesseract 5.3.4 | 1365 ms | 1648 ms | 200.4 MB | 4.1 MB | 6.4 s | 16.5 s |
| RapidOCR 1.4.4 (ONNX, CPU) | 1936 ms | 2328 ms | 671.4 MB | 16.2 MB | 5.9 s | 23.0 s |

The first-page time is much larger than the warm time and its cause was not investigated (engine initialisation and cache
warm-up are plausible, not confirmed). The load average rose to 1.33 by the end, so a later page may have shared the CPU with
the harness. Both CPU-only; no GPU was used.

The per-document latencies inside `benchmarks/results/*.json` (p50 1.4 to 2.6 s) were taken during the 25-minute accuracy
run and are not the figures to quote. Latency under concurrent load, queue behaviour and throughput are **unmeasured**.

## 3. Not measured

* Any accuracy on real invoices, receipts, scans, camera photos; handwriting; stamps; Devanagari or mixed-script text.
* PaddleOCR and EasyOCR. They were not evaluated: both need heavy dependencies (large frameworks, model downloads), and with no real data a comparison would not justify adding them. They stay candidates for when real data exists.
* Document-AI transformer models and LLM extraction (no model or consent path is available here; see ML_PIPELINE.md section 10).
* GPU behaviour, throughput, behaviour under concurrency.
* Calibration of confidence (no labelled data; see section 5).
* Skew above 1.6 degrees, 300+ dpi scans, rotated pages (90/180/270 degrees), orientation detection.
* Multi-page documents beyond the synthetic multi-page samples (every sixth document is multi-page, which is a pipeline test, not a benchmark).
* Statistical power across templates: 72 documents are 18 template-and-degradation cells of about 4 each. Per-slice numbers are anecdotal. Documents of one template share vendors, so they are not independent, and the bootstrap treats them as if they were.

## 4. Assumptions

* The synthetic ground-truth row text is the right reference for CER and WER (reading order may differ from line order in places; not checked).
* Tolerance for money matching in evaluation is 0.01 absolute; validation tolerance in production is `max(0.02, 0.0005 x amount)` (engineering judgement).
* Tesseract and RapidOCR were run with default settings. No tuning was done for either.
* The bootstrap interval (2000 resamples, seed 0) is a descriptive aid on non-independent documents, not a formal test.

## 5. Confidence

`ece_provisional` is the expected calibration error of the heuristic score against "all critical fields correct" on the
evaluated set: 0.22 for `layout-rules` and 0.425 for the baseline on Tesseract output. On synthetic data this says only that the
heuristic score is not a probability. No calibrator exists (it needs at least 500 labelled documents), so
`confidence_calibrated = false` everywhere. Do not automate decisions on `confidence`.

## 6. Custom model decision

**Training a custom OCR or document-understanding model is not justified now.**

* No representative labelled data exists, so a trained model could not be evaluated honestly. The readiness policy (1000
  training examples, 100 supplier groups, 100 validation examples, 50 per critical field) is not met.
* On the synthetic set Tesseract plus the layout-rule extractor are already near the ceiling; the remaining errors (tax
  field, line-item descriptions) are better studied on real documents.
* The right first step is to collect real documents under a consent policy, label them through the example lifecycle, and rerun this
  benchmark on a held-out set. Then decide between: tuning rules, adding a layout model behind the `LayoutAnalyzer`/`Extractor`
  interfaces, or fine-tuning.
* No trainer ships. The training wrapper, dataset builder, registry gates and runtime loader are tested with fakes.

## 7. Status labels

Terms: **implemented** = code exists; **tested** = automated tests pass; **benchmarked** = measured on the synthetic set only; **verified** = observed working against real services in the sandbox; **unverified** = not observed; **deferred** = not built.

| Component | Implemented | Tested | Benchmarked | Verified | Note |
|---|---|---|---|---|---|
| Tesseract provider, page-level OCR, multi-page aggregation | yes | yes (incl. real engine) | yes (synthetic) | yes (real Celery stack) | accuracy on real documents unknown |
| RapidOCR provider (optional) | yes | partly (adapter only) | yes (synthetic) | no | experimental; worse than Tesseract on this set |
| Preprocessing ops (`autocontrast`, `median3`, `sharpen`, `otsu`, `deskew`) | yes | yes | yes (synthetic) | no | off by default; no op shown to help extraction |
| `RuleBasedExtractor` (reference) | yes | yes | yes (synthetic) | yes | weak on split-cell OCR (measured) |
| `LayoutRulesExtractor`, heuristic layout analyser | yes | yes | yes (synthetic) | no | experimental; likely overfit to the synthetic templates |
| Normalisation, BS/AD dates, validation, evidence | yes | yes | yes (synthetic) | partly | tolerances are engineering judgement, unfitted |
| Heuristic confidence | yes | yes | ECE only | yes | uncalibrated; a flag, not a probability |
| Calibration (isotonic) | code only | yes | no | no | deferred: no labelled data |
| Consent, example lifecycle, quality gates, splits, dataset builder | yes | yes (PostgreSQL) | n/a | no | no real tenant data has passed through it |
| Evaluation harness, immutable runs, paired comparison | yes | yes | n/a | yes (benchmark) | |
| Model registry, gates, rollback, runtime loader | yes | yes (fake models) | n/a | no | no real trained model has run through it |
| Training wrapper | wrapper only | yes | no | no | no trainer exists |
| Unsupported-script flag (Devanagari characters in OCR output) | yes | yes | no | no | cannot see Devanagari through an English-only engine |
| PaddleOCR, EasyOCR, transformer document models, LLM extraction, external providers | no | no | no | no | deferred |
| Unlearning trained models after consent withdrawal | no (affected models are identified, not retrained) | yes (identification) | no | no | deferred |
| Devanagari OCR | no | no | no | no | deferred, unsupported |
| Docker build and scan, real OTel collector, kernel sandbox | partly | partly | no | no | open, see `RELEASE_AUDIT.md` |

None of this means the system is production-ready.
