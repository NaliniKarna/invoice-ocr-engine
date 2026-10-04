"""Builds immutable dataset snapshots from APPROVED training examples only (never from raw feedback).

Scope rules (tenant isolation for ML):
* scope "tenant:<client_id>"  - only that tenant's examples; tenant policy must be tenant_only or pooled.
* scope "pooled"              - only tenants whose CURRENT policy is "pooled"; one tenant is capped at
                                ml_max_tenant_share of the dataset once >= 3 tenants contribute.
Evaluation isolation:
* an evaluation dataset holds only groups that hash into the test split and never groups already used for training;
* a training dataset never contains a group that belongs to an active evaluation dataset of the same scope.
The artifact holds ground-truth fields and pseudonymous references. It never holds OCR text, file names or API keys.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import Conflict, ValidationFailed
from app.core.ids import new_id
from app.ml.splits import SplitSpec, leakage_report, split_summary
from app.ml.splits import assign
from app.models import Client, DatasetExample, TrainingDataset, TrainingExample
from app.observability import metrics as m
from app.services.audit import AuditService
from app.storage.base import ObjectStorage, validate_key


def _scope_clients(scope: str) -> tuple[str | None, tuple[str, ...]]:
    if scope == "pooled":
        return None, ("pooled",)
    if scope.startswith("tenant:") and len(scope) > 7:
        return scope[7:], ("tenant_only", "pooled")
    raise ValidationFailed("scope must be 'pooled' or 'tenant:<client_id>'", code="invalid_scope")


def _groups_of(db: Session, scope: str, kind: str) -> set[str]:
    """Groups already locked into datasets of the given kind for this scope (flagged datasets no longer count)."""
    q = (select(DatasetExample.group_key).join(TrainingDataset, TrainingDataset.id == DatasetExample.dataset_id)
         .where(TrainingDataset.scope == scope, TrainingDataset.kind == kind, TrainingDataset.status == "active"))
    return set(db.execute(q).scalars())


def build_dataset(db: Session, storage: ObjectStorage, settings: Settings, *, scope: str, kind: str = "training",
                  name: str | None = None, spec: SplitSpec = SplitSpec(), min_examples: int = 1, created_by: str = "admin",
                  pseudonymize_salt: str = "", allow_small_eval: bool = False) -> TrainingDataset | None:
    if kind not in ("training", "evaluation"):
        raise ValidationFailed("kind must be training or evaluation", code="invalid_kind")
    spec.validate()
    tenant, policies = _scope_clients(scope)
    q = (select(TrainingExample).join(Client, Client.id == TrainingExample.client_id)
         .where(TrainingExample.status == "approved", Client.status == "active", Client.training_policy.in_(policies)))
    if tenant:
        q = q.where(TrainingExample.client_id == tenant)
    rows = list(db.execute(q.order_by(TrainingExample.created_at, TrainingExample.id)).scalars())

    report: dict = {"candidates": len(rows), "excluded": defaultdict(int)}
    other = "training" if kind == "evaluation" else "evaluation"
    locked = _groups_of(db, scope, other)
    kept: list[TrainingExample] = []
    for r in rows:
        if r.group_key in locked:
            report["excluded"]["group_locked_in_" + other] += 1
        elif kind == "evaluation" and assign(r.group_key, spec) != "test":
            report["excluded"]["not_in_test_split"] += 1
        else:
            kept.append(r)

    # one tenant must not dominate a pooled dataset
    if scope == "pooled":
        tenants = {r.client_id for r in kept}
        if len(tenants) >= 3:
            cap = max(1, int(len(kept) * settings.ml_max_tenant_share))
            counts: dict[str, int] = defaultdict(int)
            capped = []
            for r in kept:
                if counts[r.client_id] < cap:
                    counts[r.client_id] += 1
                    capped.append(r)
                else:
                    report["excluded"]["tenant_share_cap"] += 1
            kept = capped

    # an exact-duplicate ground truth is kept once (copies would leak across splits)
    seen: set[str] = set()
    uniq = []
    for r in kept:
        if r.content_sha256 in seen:
            report["excluded"]["duplicate_content"] += 1
            continue
        seen.add(r.content_sha256)
        uniq.append(r)
    kept = uniq

    if len(kept) < min_examples:
        report["outcome"] = "below_min_examples"
        db.flush()
        return None

    if kind == "evaluation":
        assignments = {r.id: "test" for r in kept}
    else:
        tr_val = SplitSpec(seed=spec.seed, train=spec.train / (spec.train + spec.val) if spec.train + spec.val else 1.0,
                           val=spec.val / (spec.train + spec.val) if spec.train + spec.val else 0.0, test=0.0,
                           min_groups_per_eval_split=spec.min_groups_per_eval_split)
        assignments = {r.id: assign(r.group_key, tr_val) for r in kept}
    groups = {r.id: r.group_key for r in kept}
    leak = leakage_report(assignments, groups)
    if not leak["ok"]:
        raise Conflict("Split leakage detected; dataset not created", code="split_leakage")
    summary = split_summary(assignments, groups, spec if kind == "training" else SplitSpec(seed=spec.seed, train=0, val=0, test=1,
                                                                                         min_groups_per_eval_split=spec.min_groups_per_eval_split))
    if kind == "evaluation" and summary["warnings"] and not allow_small_eval:
        report["outcome"] = "evaluation_set_too_small"
        report["warnings"] = summary["warnings"]
        return None

    def tref(cid: str) -> str:
        return hashlib.sha256((pseudonymize_salt + cid).encode()).hexdigest()[:16]

    lines = [json.dumps({
        "example_id": r.id, "split": assignments[r.id], "group": r.group_key, "tenant_ref": tref(r.client_id),
        "schema_version": r.schema_version, "truth": r.truth, "machine_result_sha256": r.machine_result_sha256,
        "revision": r.revision, "source": r.source}, sort_keys=True, separators=(",", ":")) for r in kept]
    blob = ("\n".join(lines) + "\n").encode()
    ds_id = new_id("dataset")
    uri = validate_key(f"datasets/{ds_id}.jsonl")
    storage.put(uri, blob, "application/x-ndjson")
    ds = TrainingDataset(
        id=ds_id, artifact_uri=uri, record_count=len(kept), checksum_sha256=hashlib.sha256(blob).hexdigest(),
        name=name, kind=kind, scope=scope, status="active", schema_version=max(r.schema_version for r in kept),
        split_spec={"seed": spec.seed, "train": spec.train, "val": spec.val, "test": spec.test},
        created_by=created_by,
        quality_report={**summary, "candidates": report["candidates"], "excluded": dict(report["excluded"]),
                        "tenants": len({r.client_id for r in kept}), "leakage": leak,
                        "synthetic": False},
        filters={"status": "approved", "policies": list(policies)})
    db.add(ds)
    db.flush()
    for r in kept:
        db.add(DatasetExample(id=new_id("dsex"), dataset_id=ds.id, example_id=r.id, split=assignments[r.id], group_key=r.group_key))
    AuditService(db).record(action="dataset.build", resource_type="dataset", resource_id=ds.id, actor="admin",
                            details={"kind": kind, "scope": scope, "records": len(kept), "by": created_by})
    m.ML_DATASETS.labels(kind=kind, scope_type="pooled" if scope == "pooled" else "tenant").inc()
    db.flush()
    return ds


def load_dataset(storage: ObjectStorage, ds: TrainingDataset) -> list[dict]:
    """Reads a dataset artifact and verifies its checksum (a tampered artifact is rejected, not trained on)."""
    blob = storage.get(ds.artifact_uri)
    if hashlib.sha256(blob).hexdigest() != ds.checksum_sha256:
        raise Conflict("Dataset artifact checksum mismatch", code="dataset_corrupt")
    return [json.loads(line) for line in blob.decode().splitlines() if line.strip()]
