"""Model registry with gated promotion. Feedback never changes production weights; a candidate must carry immutable
evaluation evidence, pass configurable gates (including a no-regression check against the current production model)
and be explicitly approved before it can become the single PRODUCTION model of its kind.

    candidate -> evaluated -> approved -> production -> retired       (rejected is reachable from any pre-production state)
    retired -> production  is allowed ONLY through rollback() (it is an audited, explicit operator action)

If no model of a kind is in production, callers use the reference implementation (regex extractor / configured OCR).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.core.ids import new_id
from app.core.time import utcnow
from app.models import EvaluationRun, ModelVersion
from app.services.audit import AuditService

CANDIDATE, EVALUATED, APPROVED, PRODUCTION, RETIRED, REJECTED = (
    "candidate", "evaluated", "approved", "production", "retired", "rejected")

TRANSITIONS = {
    CANDIDATE: {EVALUATED, REJECTED},
    EVALUATED: {EVALUATED, APPROVED, REJECTED},
    APPROVED: {PRODUCTION, REJECTED, EVALUATED},
    PRODUCTION: {RETIRED},
    RETIRED: set(),          # re-activation goes through rollback()
    REJECTED: set(),
}


@dataclass(frozen=True)
class PromotionGates:
    """Every threshold is configuration. The defaults are deliberately strict; none is tuned on real data (there is none)."""
    min_samples: int = 200                       # below this the evaluation cannot approve anything
    allow_synthetic_evidence: bool = False       # synthetic benchmarks can rank candidates but never approve them
    primary_metric: str = "field_f1"
    min_primary: float = 0.0                     # absolute floor for the primary metric
    min_improvement: float = 0.0                 # must beat production/reference on the primary metric by this much
    max_regression: float = 0.0                  # no guarded metric may fall more than this below production/reference
    guarded_metrics: tuple[str, ...] = ("field_f1", "critical_field_f1", "total_exact", "date_exact", "line_item_f1")
    max_latency_ratio: float | None = 2.0        # candidate p95 latency / production p95 latency
    require_same_dataset: bool = True            # comparison only valid on the identical evaluation data
    require_schema_match: str | None = None      # e.g. current extraction schema version


@dataclass
class GateResult:
    passed: bool
    failures: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)

    def fail(self, code: str, **info) -> None:
        self.passed = False
        self.failures.append({"code": code, **info})


def evaluate_gates(candidate: EvaluationRun, reference: EvaluationRun | None, gates: PromotionGates,
                   *, candidate_schema: str | None = None) -> GateResult:
    """Pure comparison of two evaluation runs. `reference` is the current production model's run, or the reference
    extractor's run when nothing is in production. Missing reference => candidate is only checked against absolutes."""
    r = GateResult(True)
    if candidate.synthetic and not gates.allow_synthetic_evidence:
        r.fail("synthetic_evidence_not_allowed")
    if candidate.sample_count < gates.min_samples:
        r.fail("insufficient_samples", have=candidate.sample_count, need=gates.min_samples)
    if gates.require_schema_match and candidate_schema != gates.require_schema_match:
        r.fail("schema_mismatch", have=candidate_schema, need=gates.require_schema_match)
    cm = candidate.metrics or {}
    prim = cm.get(gates.primary_metric)
    if prim is None:
        r.fail("missing_metric", metric=gates.primary_metric)
    elif prim < gates.min_primary:
        r.fail("below_floor", metric=gates.primary_metric, value=prim, floor=gates.min_primary)
    if reference is None:
        r.fail("no_reference_evaluation")     # never approve without something to compare against
        return r
    if gates.require_same_dataset and reference.dataset_sha256 != candidate.dataset_sha256:
        r.fail("different_evaluation_dataset")
        return r
    rm = reference.metrics or {}
    if prim is not None and rm.get(gates.primary_metric) is not None:
        gain = prim - rm[gates.primary_metric]
        r.checks.append({"metric": gates.primary_metric, "delta": round(gain, 6)})
        if gain < max(gates.min_improvement, 1e-9):      # must strictly beat the reference
            r.fail("no_improvement", metric=gates.primary_metric, delta=round(gain, 6), needed=gates.min_improvement)
    for name in gates.guarded_metrics:
        if name in cm and name in rm and cm[name] is not None and rm[name] is not None:
            delta = cm[name] - rm[name]
            r.checks.append({"metric": name, "delta": round(delta, 6)})
            if delta < -gates.max_regression:
                r.fail("regression", metric=name, delta=round(delta, 6), allowed=-gates.max_regression)
    if gates.max_latency_ratio is not None:
        cl, rl = cm.get("latency_p95_ms"), rm.get("latency_p95_ms")
        if cl is not None and rl:
            if cl / rl > gates.max_latency_ratio:
                r.fail("latency_regression", ratio=round(cl / rl, 3), allowed=gates.max_latency_ratio)
    return r


class ModelRegistry:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    # ---------------------------------------------------------------- registration / lookup
    def register_candidate(self, *, kind: str, name: str, version: str, artifact_uri: str | None,
                           dataset_id: str | None, parent_id: str | None = None, schema_version: str | None = None,
                           artifact_sha256: str | None = None, signature: str | None = None,
                           config: dict | None = None, training_meta: dict | None = None) -> ModelVersion:
        mv = ModelVersion(id=new_id("model"), kind=kind, name=name, version=version, status=CANDIDATE,
                          artifact_uri=artifact_uri, dataset_id=dataset_id, parent_id=parent_id,
                          schema_version=schema_version, artifact_sha256=artifact_sha256, signature=signature,
                          config=config or {}, training_meta=training_meta or {})
        self.db.add(mv)
        self.db.flush()
        self.audit.record(action="model.register", resource_type="model", resource_id=mv.id, actor="admin",
                          details={"kind": kind, "name": name, "version": version})
        return mv

    def _get(self, mv_id: str) -> ModelVersion:
        mv = self.db.get(ModelVersion, mv_id)
        if mv is None:
            raise NotFound("Model version not found")
        return mv

    def production(self, kind: str) -> ModelVersion | None:
        return self.db.execute(select(ModelVersion).where(ModelVersion.kind == kind, ModelVersion.status == PRODUCTION)
                               ).scalar_one_or_none()

    def latest_run(self, model_id: str, *, split: str = "test", dataset_sha256: str | None = None) -> EvaluationRun | None:
        q = select(EvaluationRun).where(EvaluationRun.model_version_id == model_id, EvaluationRun.split == split)
        if dataset_sha256:
            q = q.where(EvaluationRun.dataset_sha256 == dataset_sha256)
        return self.db.execute(q.order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc()).limit(1)).scalar_one_or_none()

    def _move(self, mv: ModelVersion, dst: str, reason: str | None = None) -> None:
        if dst not in TRANSITIONS[mv.status]:
            raise Conflict(f"Illegal model transition {mv.status} -> {dst}", code="invalid_model_transition")
        self.audit.record(action=f"model.{dst}", resource_type="model", resource_id=mv.id, actor="admin",
                          details={"from": mv.status, "kind": mv.kind, "reason": reason})
        mv.status = dst
        if reason:
            mv.status_reason = reason[:500]

    # ---------------------------------------------------------------- evaluation
    def record_evaluation(self, mv_id: str, metrics: dict | None = None, *, run: EvaluationRun | None = None) -> ModelVersion:
        """Legacy form stores a metrics dict. Preferred form passes an immutable EvaluationRun (already persisted)."""
        mv = self._get(mv_id)
        if mv.status == CANDIDATE:
            self._move(mv, EVALUATED)
        elif mv.status not in (EVALUATED, APPROVED):
            raise Conflict(f"Cannot evaluate a {mv.status} model", code="invalid_model_transition")
        if run is not None:
            if run.model_version_id != mv.id:
                raise Conflict("Evaluation run belongs to another model", code="evaluation_mismatch")
            metrics = {**(metrics or {}), **(run.metrics or {})}
        mv.metrics = {**(mv.metrics or {}), **(metrics or {})}
        return mv

    # ---------------------------------------------------------------- approval
    def approve(self, mv_id: str, *, baseline_metric: float, metric_name: str = "field_f1",
                min_improvement: float = 0.0) -> ModelVersion:
        """Single-metric gate against a caller-supplied number. Kept for compatibility; it does NOT check sample size,
        synthetic evidence, regressions or dataset identity. Use approve_with_gates() for anything that matters."""
        mv = self._get(mv_id)
        score = (mv.metrics or {}).get(metric_name)
        if score is None or score < baseline_metric + min_improvement:
            self._move(mv, REJECTED, "did not beat baseline")
            raise Conflict("Candidate did not beat the baseline", code="candidate_rejected")
        self._move(mv, APPROVED)
        return mv

    def approve_with_gates(self, mv_id: str, gates: PromotionGates, *, reference_run_id: str | None = None,
                           approved_by: str = "admin", reject_on_failure: bool = False) -> GateResult:
        """Compares the candidate's latest test-split run with the production model's run on the SAME dataset.
        Without a production model, `reference_run_id` must identify the reference extractor's run on that dataset."""
        mv = self._get(mv_id)
        if mv.status != EVALUATED:
            raise Conflict("Only evaluated models can be approved", code="invalid_model_transition")
        cand = self.latest_run(mv.id)
        if cand is None:
            raise Conflict("Model has no evaluation run", code="no_evaluation")
        ref: EvaluationRun | None = None
        prod = self.production(mv.kind)
        if reference_run_id:
            ref = self.db.get(EvaluationRun, reference_run_id)
        elif prod is not None:
            ref = self.latest_run(prod.id, dataset_sha256=cand.dataset_sha256)
        res = evaluate_gates(cand, ref, gates, candidate_schema=mv.schema_version)
        self.audit.record(action="model.gate_check", resource_type="model", resource_id=mv.id, actor="admin",
                          details={"passed": res.passed, "failures": res.failures, "by": approved_by,
                                   "run": cand.id, "reference_run": ref.id if ref else None})
        if res.passed:
            self._move(mv, APPROVED, f"approved by {approved_by}")
        elif reject_on_failure:
            self._move(mv, REJECTED, "gates failed: " + ",".join(f["code"] for f in res.failures)[:400])
        return res

    # ---------------------------------------------------------------- promotion / rollback
    def promote(self, mv_id: str, *, promoted_by: str) -> ModelVersion:
        mv = self._get(mv_id)
        if mv.status != APPROVED:
            raise Conflict("Only approved models can be promoted", code="invalid_model_transition")
        self._swap(mv, promoted_by, "model.promote")
        return mv

    def _swap(self, mv: ModelVersion, by: str, action: str) -> None:
        prev = self.production(mv.kind)
        if prev is not None and prev.id != mv.id:
            prev.status = RETIRED
            self.db.flush()                      # free the partial unique slot before activating the new model
        self.audit.record(action=action, resource_type="model", resource_id=mv.id, actor="admin",
                          details={"kind": mv.kind, "from_production": prev.id if prev else None, "by": by})
        mv.status, mv.promoted_at, mv.promoted_by = PRODUCTION, utcnow(), by
        self.db.flush()                          # partial unique index guarantees a single production model per kind

    def rollback(self, kind: str, to_id: str, *, by: str) -> ModelVersion:
        """Re-activate a previously promoted (now retired) model. The model must still pass integrity checks when loaded."""
        mv = self._get(to_id)
        if mv.kind != kind or mv.promoted_at is None or mv.status not in (RETIRED, PRODUCTION):
            raise Conflict("Model was never in production", code="invalid_model_transition")
        self._swap(mv, by, "model.rollback")
        return mv

    def retire(self, mv_id: str, *, reason: str = "manual") -> ModelVersion:
        """Take the production model out of service; callers fall back to the reference implementation."""
        mv = self._get(mv_id)
        self._move(mv, RETIRED, reason)
        return mv
