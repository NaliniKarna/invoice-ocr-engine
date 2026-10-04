# Invoice review web UI

A small internal tool for contributors: **Upload → read → review → correct → Submit → next invoice.** It is not a customer
product. It adds no OCR, extraction, storage or feedback logic; it calls the existing Bill OCR API.

## How it connects

```
browser ──(same origin, no key, no cookies)──> webui (app/webui, port 8080) ──X-API-Key──> API (/api/v1/bills...)
```

* `webui` is a separate small FastAPI service. It has no access to the database, object storage or queue.
* It holds one secret: the collection client's API key (`OCR_UI_API_KEY`). The browser never receives it. The browser also never
  sees process IDs, tenant IDs or API paths: invoices are referenced by opaque, tamper-evident tokens derived from the key.
* API calls used: `POST /api/v1/bills` (upload), `GET /api/v1/bills` (list), `GET /api/v1/bills/{id}` (result),
  `GET /api/v1/bills/{id}/file` (original), `POST /api/v1/bills/{id}/feedback` (corrections / confirmation).
* The original extraction is never changed. On Submit the web service fetches the stored result from the API itself and sends only
  the fields that differ as feedback. If nothing differs, it sends `reviewed: true` with no corrections: the invoice is recorded as
  "a person confirmed this", which is different from "no issues detected".
* No sign-in, no sessions, no cookies. If the deployment needs access protection (VPN, SSO proxy, basic auth at the reverse proxy),
  put it in front of the `webui` port; the application does not know about it.

## Backend additions (existing API, same security model)

| Change | Why |
| --- | --- |
| `GET /api/v1/bills` (newest first, cursor paging, `review=pending\|reviewed`) | the invoice list |
| `GET /api/v1/bills/{id}/file` (tenant-scoped, `no-store`, `nosniff`) | show the original; no public storage URLs |
| `reviewed` flag on `POST .../feedback`; new `reviews` table (migration `0005`) | record "confirmed correct" and "reviewed with corrections" once per invoice (second attempt: 409) |
| ML example proposal accepts a confirmation (`source = client_confirmation`) | reviewed-correct invoices can become examples; they still pass quality gates and operator review |

Feedback without `reviewed` behaves exactly as before.

## Run it

1. Apply migrations (`docker compose up -d` runs `migrate`).
2. Create the collection client and key (operator tool, as for any client). The key needs scopes `bills:write`, `bills:read`,
   `feedback:write` (the defaults).
3. Set `OCR_UI_API_KEY` in your shell or a compose-level `.env`, then:
   `docker compose --profile ui up -d --build webui` and open http://127.0.0.1:8080.

## Environment variables (webui service only)

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCR_UI_API_KEY` | none, required | collection client API key (min. 20 chars). Server side only |
| `OCR_UI_API_BASE_URL` | `http://api:8000` | where the API is |
| `OCR_UI_MAX_UPLOAD_BYTES` | 10 MiB | per-file limit; keep at or below the API limit |
| `OCR_UI_MAX_FILES_PER_UPLOAD` | 20 | files per drop |
| `OCR_UI_FEEDBACK_NOTICE` | "Your corrections may help improve invoice extraction accuracy." | text under Submit; empty hides it |
| `OCR_UI_ALLOWED_ORIGINS` | empty | extra origins allowed to POST, only if a proxy rewrites `Host` |
| `OCR_UI_REQUEST_TIMEOUT_SECONDS` | 30 | upstream timeout |

## Decisions the operator must make

* **Collection client training policy.** The default is `none`: corrections are stored but cannot become training examples.
  Set `tenant_only` or `pooled` for the collection client, and raise `retention_days` (default 30 days; purge deletes derived
  examples unless pooled). Make the notice text match the policy and tell contributors before they upload.
* **Plan limits** for the collection client (daily uploads, concurrency) apply to everything the UI uploads.

## Limits (v1)

* Everyone using the tool sees every invoice of the collection client; there is no per-person view or attribution. Protect access at deployment level.
* A correction can add at most one new line-item row per submission; existing rows cannot be deleted (existing feedback rules).
* Contributors cannot delete invoices; use the API or the retention purge.
* Only PNG, JPG and PDF. PDFs are shown by the browser's built-in viewer; the Chromium PDF viewer path was not exercised in the sandbox.
* The Docker image and compose service were not built or run in the development sandbox (no Docker there).
