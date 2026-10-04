"""Deterministic, leak-free dataset splitting.

The unit of assignment is the GROUP (supplier/template family within a tenant, see quality.group_key_for), never the
individual document, so near-identical invoices cannot land on both sides of a train/evaluation boundary. The
assignment depends only on (seed, group_key): adding new documents never moves an existing group to another split.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

SPLITS = ("train", "val", "test")


@dataclass(frozen=True)
class SplitSpec:
    seed: str = "0"
    train: float = 0.8
    val: float = 0.1
    test: float = 0.1
    min_groups_per_eval_split: int = 5     # below this a split is reported as statistically meaningless

    def validate(self) -> None:
        if min(self.train, self.val, self.test) < 0 or abs(self.train + self.val + self.test - 1.0) > 1e-9:
            raise ValueError("split ratios must be non-negative and sum to 1")


def bucket(seed: str, group_key: str) -> float:
    h = hashlib.sha256(f"{seed}|{group_key}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def assign(group_key: str, spec: SplitSpec) -> str:
    b = bucket(spec.seed, group_key)
    if b < spec.train:
        return "train"
    return "val" if b < spec.train + spec.val else "test"


def split_examples(items: list[tuple[str, str]], spec: SplitSpec) -> dict[str, str]:
    """items = [(example_id, group_key)] -> {example_id: split}."""
    spec.validate()
    return {eid: assign(gk, spec) for eid, gk in items}


def leakage_report(assignments: dict[str, str], groups: dict[str, str]) -> dict:
    """Groups that appear in more than one split. Must be empty for a valid dataset."""
    seen: dict[str, set[str]] = {}
    for eid, sp in assignments.items():
        seen.setdefault(groups[eid], set()).add(sp)
    leaked = sorted(g for g, s in seen.items() if len(s) > 1)
    return {"leaked_groups": leaked, "ok": not leaked}


def split_summary(assignments: dict[str, str], groups: dict[str, str], spec: SplitSpec) -> dict:
    counts = {s: 0 for s in SPLITS}
    gcounts: dict[str, set[str]] = {s: set() for s in SPLITS}
    for eid, sp in assignments.items():
        counts[sp] += 1
        gcounts[sp].add(groups[eid])
    warnings = []
    for s in ("val", "test"):
        if getattr(spec, s) > 0 and len(gcounts[s]) < spec.min_groups_per_eval_split:
            warnings.append(f"{s} split has {len(gcounts[s])} groups (< {spec.min_groups_per_eval_split}): "
                            "metrics from it are not statistically meaningful")
    return {"examples": counts, "groups": {s: len(v) for s, v in gcounts.items()}, "warnings": warnings}
