# Security scans actually performed

Latest run: 2026-10-01 (release audit). Environment: Python 3.11.15, Linux sandbox, outbound HTTPS only through a policy proxy.

| Tool | Version | Target | Result |
|---|---|---|---|
| pip-audit | 2.10.1 | `constraints.txt` with `--no-deps --disable-pip` (the pinned set resolved on 2026-09-30 from a clean venv: `requirements.txt` + `requirements-ocr-tesseract.txt`, plus the `numpy` and `nepali-datetime` pins added later by hand) | No known vulnerabilities found |
| bandit | 1.9.4 | `app/` (about 5,400 statements measured by coverage; bandit reports its own line count) | 0 high, 0 medium, 8 low after the audit (9 before; see below) |
| detect-secrets | 1.5.0 | whole tree, `--all-files` (the tree is not a git repository, so git-aware mode scans nothing), excluding `constraints.txt`, `benchmarks/results`, `__pycache__` | 14 candidates, all false positives (see below) |

Bandit low findings, all reviewed and accepted:
* B110 `try/except/pass` in `app/observability/tracing.py` (3 places): tracing must never break a request.
* B404/B603 `subprocess` in `app/security/sandbox.py`: fixed argv (`sys.executable -m <internal module>`), no shell, untrusted data only on stdin, empty environment.
* B311 `random` in `app/workers/job.py` (retry jitter), `app/ml/calibration.py` and `app/ml/evaluation.py` (bootstrap resampling): none security-sensitive.
Fixed during the audit: B101 `assert` used for type narrowing in `app/ocr/base.py` (replaced by an explicit condition).

detect-secrets candidates (all reviewed): development database URL `ocr:ocr` in `.env.example`, `README.md` and the default in `app/core/config.py` (production settings refuse it; tested), `"api_key": "key"` id prefix in `app/core/ids.py`, two `docker-compose.yml` volume lines, and deliberately fake credentials in tests (`TOP-SECRET-VALUE-123`, `s3cret`, `SECRETPW`, `realsecret`). No real secret was found.
Correction: an earlier version of this file reported "0 findings" for detect-secrets. That result cannot be reproduced as stated: without git the default mode scans no files. Treat the run above as the valid one.

## Not run
* **Trivy / Grype / any container image scan**: no image could be built. A Docker daemon starts in the sandbox, but pulls from Docker Hub, `mirror.gcr.io`, `public.ecr.aws`, `quay.io` and `ghcr.io` are all denied (HTTP 403 from the egress policy), so the `python:3.11-slim` base is unavailable. The Trivy binary could not be downloaded either.
* **gitleaks**: binary not installable here (GitHub release download denied; no PyPI distribution). detect-secrets was used instead.
* pip-audit of `requirements-ocr-rapidocr.txt` (optional engine) and of OS packages in the future images.
* DAST or penetration test by an independent party. The hostile tests in `tests/integration/test_final_audit.py` are in-process tests, not a penetration test.

Re-run all of these in CI on every change.
