# Benchmark result files

All files are SYNTHETIC-data results (seeded generator in `benchmarks/synthetic.py`). They say nothing about real invoices.
Files are immutable history: never edit, add a new run instead.

* `20260930T190939Z-b84ce3b5e129.json` run 1 (seed 7, n=72). **Superseded.** Produced before the deskew fix: the old
  `estimate_skew_degrees` mis-rotated low-contrast pages by up to -5 degrees. Its `deskew` rows (and the extractor results built on
  them) describe the buggy operation, not the shipped one. The `none`, `autocontrast`, `median3,otsu` rows are unaffected
  by that bug. Latencies in this run were contaminated by concurrent test runs; do not use them.
* `20260930T193419Z-e852ba16f3e3.json` run 2 (seed 7, n=72), after the deskew fix. Use this one for accuracy comparisons.
  Its latencies were measured with a quiet machine except for the benchmark itself; for latency and memory claims use the
  `resources-*` file instead.
* `resources-20261001T021614Z-8531c631c7.json` isolated per-engine resource run (12 pages, one engine per process, idle machine, 2 CPUs).
