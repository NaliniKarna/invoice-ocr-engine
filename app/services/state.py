"""Process state machine. Transitions are enforced in SQL (compare-and-swap) so concurrent
workers/reapers/API calls cannot double-apply them.

    queued ──► processing ──► completed
      │  ▲          │ ├──────► failed
      │  └──────────┘ (retry)
      └──► cancelled            (only while still queued)
"""
from __future__ import annotations

QUEUED, PROCESSING, COMPLETED, FAILED, CANCELLED = "queued", "processing", "completed", "failed", "cancelled"
ALL = (QUEUED, PROCESSING, COMPLETED, FAILED, CANCELLED)
ACTIVE = (QUEUED, PROCESSING)
TERMINAL = (COMPLETED, FAILED, CANCELLED)

ALLOWED: dict[str, frozenset[str]] = {
    QUEUED: frozenset({PROCESSING, CANCELLED}),
    PROCESSING: frozenset({COMPLETED, FAILED, QUEUED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    return dst in ALLOWED.get(src, frozenset())


def sources_for(dst: str) -> list[str]:
    return [s for s, dsts in ALLOWED.items() if dst in dsts]
