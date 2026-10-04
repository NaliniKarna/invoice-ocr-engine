"""Invoice review web layer for contributors: upload, check the extracted values against the original, correct, submit.

This is a thin front for the existing Bill OCR API. It owns no invoice data, runs no OCR, and has no accounts. It holds the
one collection API key (from the environment) and attaches it to server-to-server calls; the browser never sees it."""
