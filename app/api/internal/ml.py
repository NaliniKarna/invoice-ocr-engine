"""Operator-only ML governance API (same X-Admin-Token as the rest of /internal). Not exposed to tenants:
a tenant API key is rejected here because it is a different credential. Responses never contain document content."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_container, get_db
from app.api.internal.admin import admin_auth
from app.core.container import Container
from app.core.errors import NotFound
from app.ml.dataset import build_dataset
from app.ml.examples import ExampleService
from app.ml.registry import ModelRegistry, PromotionGates
from app.ml.splits import SplitSpec
from app.models import EvaluationRun, ModelVersion, TrainingDataset, TrainingExample

router = APIRouter(prefix="/internal/v1", tags=["internal-ml"], dependencies=[Depends(admin_auth)])


class PolicyBody(BaseModel):
    policy: str = Field(pattern="^(none|tenant_only|pooled)$")


class ProposeBody(BaseModel):
    client_id: str
    process_id: str


class ReviewBody(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reviewer: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=200)


class DatasetBody(BaseModel):
    scope: str = Field(max_length=80)
    kind: str = Field(default="training", pattern="^(training|evaluation)$")
    name: str | None = Field(default=None, max_length=100)
    seed: str = Field(default="0", max_length=64)
    min_examples: int = Field(default=1, ge=1)
    allow_small_eval: bool = False


class GateBody(BaseModel):
    min_samples: int = Field(default=200, ge=1)
    min_improvement: float = Field(default=0.0, ge=0, le=1)
    max_regression: float = Field(default=0.0, ge=0, le=1)
    reference_run_id: str | None = None
    reject_on_failure: bool = False
    approved_by: str = Field(default="admin", max_length=100)


class ByBody(BaseModel):
    by: str = Field(default="admin", max_length=100)


def _example(e: TrainingExample) -> dict:
    return {"example_id": e.id, "client_id": e.client_id, "process_id": e.process_id, "status": e.status, "revision": e.revision,
            "source": e.source, "issues": [{"code": i["code"], "severity": i["severity"], "field": i.get("field")}
                                           for i in (e.quality_report or {}).get("issues", [])],
            "reviewed_by": e.reviewed_by, "schema_version": e.schema_version}


def _dataset(d: TrainingDataset) -> dict:
    return {"dataset_id": d.id, "kind": d.kind, "scope": d.scope, "status": d.status, "records": d.record_count,
            "checksum_sha256": d.checksum_sha256, "schema_version": d.schema_version, "quality": d.quality_report}


def _model(mv: ModelVersion, impact: dict | None = None) -> dict:
    out = {"model_id": mv.id, "kind": mv.kind, "name": mv.name, "version": mv.version, "status": mv.status,
           "schema_version": mv.schema_version, "promoted_at": mv.promoted_at, "promoted_by": mv.promoted_by,
           "metrics": mv.metrics, "status_reason": mv.status_reason, "dataset_id": mv.dataset_id, "parent_id": mv.parent_id}
    if impact is not None:
        out.update(impact)
    return out


def withdrawal_impact(db: Session, models: list[ModelVersion]) -> dict[str, dict]:
    """Which models were trained on data that has since been withdrawn (dataset flagged), directly or through a parent.
    The system does NOT retrain or unlearn automatically; this only identifies what an operator must rebuild and retrain."""
    by_id = {m.id: m for m in db.execute(select(ModelVersion)).scalars()}
    flagged = {d.id for d in db.execute(select(TrainingDataset).where(TrainingDataset.status == "flagged_withdrawal")).scalars()}
    out: dict[str, dict] = {}
    for m in models:
        chain, cur, seen = [], m, set()
        while cur is not None and cur.id not in seen and len(seen) < 25:     # bounded walk up the lineage
            seen.add(cur.id)
            if cur.dataset_id in flagged:
                chain.append(cur.dataset_id)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        out[m.id] = {"affected_by_withdrawal": bool(chain), "flagged_datasets": sorted(set(chain)),
                     "action_required": "rebuild the dataset without the withdrawn data and retrain; no automatic unlearning" if chain else None}
    return out


@router.put("/clients/{client_id}/training-policy")
def set_policy(client_id: str, body: PolicyBody, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    cl = ExampleService(db, c.settings).set_policy(client_id, body.policy, by="admin")
    db.commit()
    return {"client_id": cl.id, "training_policy": cl.training_policy}


@router.post("/examples", status_code=201)
def propose(body: ProposeBody, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    ex = ExampleService(db, c.settings).propose(body.client_id, body.process_id)
    db.commit()
    return _example(ex)


@router.get("/examples")
def list_examples(client_id: str | None = None, status: str | None = None, limit: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db)):
    q = select(TrainingExample).order_by(TrainingExample.created_at.desc()).limit(limit)
    if client_id:
        q = q.where(TrainingExample.client_id == client_id)
    if status:
        q = q.where(TrainingExample.status == status)
    return {"items": [_example(e) for e in db.execute(q).scalars()]}


@router.post("/examples/{example_id}/review")
def review(example_id: str, body: ReviewBody, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    ex = ExampleService(db, c.settings).review(example_id, body.decision, reviewer=body.reviewer, note=body.note)
    db.commit()
    return _example(ex)


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetBody, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    ds = build_dataset(db, c.storage, c.settings, scope=body.scope, kind=body.kind, name=body.name,
                       spec=SplitSpec(seed=body.seed), min_examples=body.min_examples, allow_small_eval=body.allow_small_eval)
    if ds is None:
        return {"created": False, "reason": "below minimum size or evaluation set too small"}
    db.commit()
    return {"created": True, **_dataset(ds)}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, db: Session = Depends(get_db)):
    d = db.get(TrainingDataset, dataset_id)
    if d is None:
        raise NotFound("Dataset not found")
    return _dataset(d)


@router.get("/models")
def list_models(kind: str | None = None, db: Session = Depends(get_db)):
    q = select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(200)
    if kind:
        q = q.where(ModelVersion.kind == kind)
    models = list(db.execute(q).scalars())
    impact = withdrawal_impact(db, models)
    return {"items": [_model(m, impact[m.id]) for m in models]}


@router.get("/models/{model_id}/evaluations")
def model_evaluations(model_id: str, db: Session = Depends(get_db)):
    runs = db.execute(select(EvaluationRun).where(EvaluationRun.model_version_id == model_id)
                      .order_by(EvaluationRun.created_at.desc()).limit(50)).scalars()
    return {"items": [{"run_id": r.id, "split": r.split, "samples": r.sample_count, "synthetic": r.synthetic,
                       "dataset_sha256": r.dataset_sha256, "metrics": r.metrics, "created_at": r.created_at} for r in runs]}


@router.post("/models/{model_id}/approve")
def approve(model_id: str, body: GateBody, db: Session = Depends(get_db), c: Container = Depends(get_container)):
    from app.extraction.schema import SCHEMA_VERSION
    gates = PromotionGates(min_samples=body.min_samples, min_improvement=body.min_improvement,
                           max_regression=body.max_regression, require_schema_match=SCHEMA_VERSION)
    res = ModelRegistry(db).approve_with_gates(model_id, gates, reference_run_id=body.reference_run_id,
                                               approved_by=body.approved_by, reject_on_failure=body.reject_on_failure)
    db.commit()
    return {"passed": res.passed, "failures": res.failures, "checks": res.checks}


@router.post("/models/{model_id}/promote")
def promote(model_id: str, body: ByBody, db: Session = Depends(get_db)):
    mv = ModelRegistry(db).promote(model_id, promoted_by=body.by)
    db.commit()
    return _model(mv)


@router.post("/models/{model_id}/rollback")
def rollback(model_id: str, body: ByBody, db: Session = Depends(get_db)):
    reg = ModelRegistry(db)
    mv = reg.rollback(reg._get(model_id).kind, model_id, by=body.by)
    db.commit()
    return _model(mv)


@router.post("/models/{model_id}/retire")
def retire(model_id: str, db: Session = Depends(get_db)):
    mv = ModelRegistry(db).retire(model_id, reason="manual retire")
    db.commit()
    return _model(mv)
