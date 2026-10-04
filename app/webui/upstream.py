"""The only place that talks to the Bill OCR API. Attaches the API key; turns every failure into a small, safe category.
Nothing returned from here carries the API's error text, codes or identifiers to the browser."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("webui.upstream")


@dataclass
class UpstreamError(Exception):
    kind: str                     # busy | rejected | not_found | conflict | invalid | misconfigured
    status: int | None = None
    code: str | None = None       # the API's machine code (server side only)
    details: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.kind} status={self.status} code={self.code}"


def _classify(status: int) -> str:
    if status in (401, 403):
        return "misconfigured"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status in (400, 413, 415, 422):
        return "invalid"
    if status == 429 or status >= 500:
        return "busy"
    return "rejected"


class BillApi:
    def __init__(self, http: httpx.Client, api_key: str):
        self._http, self._key = http, api_key

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, headers={"X-API-Key": self._key}, **kw)
        except httpx.HTTPError as exc:
            log.warning("API unreachable: %s", type(exc).__name__)
            raise UpstreamError("busy") from None
        if r.status_code >= 400:
            code, details = None, []
            try:
                err = r.json().get("error", {})
                code, details = err.get("code"), err.get("details") or []
            except Exception:
                pass
            kind = _classify(r.status_code)
            if kind == "misconfigured":
                log.error("API rejected the collection key (HTTP %s): check OCR_UI_API_KEY and its scopes", r.status_code)
            elif kind == "busy":
                log.warning("API busy or failing (HTTP %s %s)", r.status_code, code)
            raise UpstreamError(kind, r.status_code, code, details)
        return r

    def submit(self, filename: str, data: bytes, content_type: str) -> str:
        r = self._call("POST", "/api/v1/bills", files={"file": (filename, data, content_type)})
        return r.json()["process_id"]

    def list(self, *, limit: int = 50, cursor: str | None = None, review: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if review:
            params["review"] = review
        return self._call("GET", "/api/v1/bills", params=params).json()

    def detail(self, process_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/bills/{process_id}").json()

    def file(self, process_id: str) -> tuple[bytes, str]:
        r = self._call("GET", f"/api/v1/bills/{process_id}/file")
        return r.content, r.headers.get("content-type", "application/octet-stream")

    def review(self, process_id: str, corrections: list[dict], comment: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"corrections": corrections, "reviewed": True}
        if comment:
            body["comment"] = comment
        return self._call("POST", f"/api/v1/bills/{process_id}/feedback", json=body).json()
