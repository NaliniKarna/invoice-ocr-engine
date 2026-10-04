from __future__ import annotations

from dataclasses import dataclass, field

from app.models import Client, Plan


@dataclass(frozen=True)
class Principal:
    """Who is calling, resolved server-side from the API key. The ONLY source of tenant identity."""
    client: Client
    plan: Plan
    api_key_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    ip: str | None = None

    @property
    def client_id(self) -> str:
        return self.client.id
