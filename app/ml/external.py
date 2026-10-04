"""External (third-party) document-understanding providers: DENIED BY DEFAULT.

No provider is implemented in this code base. This module is the single place that decides whether document content
may leave the deployment, so that any future provider integration has to pass through it:
  1. the operator enabled external providers (Settings.ml_external_providers_enabled), AND
  2. the provider name is on the operator allow-list, AND
  3. the tenant explicitly consented (Client.external_processing).
Every decision is returned to the caller, who must audit it. Content, API keys and OCR text never go into the decision.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class ExternalDecision:
    allowed: bool
    reason: str


def decide(settings: Settings, *, provider: str, tenant_consented: bool) -> ExternalDecision:
    if not settings.ml_external_providers_enabled:
        return ExternalDecision(False, "external_providers_disabled")
    allow = {p.strip() for p in settings.ml_external_provider_allowlist.split(",") if p.strip()}
    if provider not in allow:
        return ExternalDecision(False, "provider_not_allow_listed")
    if not tenant_consented:
        return ExternalDecision(False, "tenant_not_consented")
    return ExternalDecision(True, "allowed")
