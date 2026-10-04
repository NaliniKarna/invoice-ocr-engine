"""Training-example lifecycle. A correction is evidence, not ground truth: it becomes a *proposed* example, passes the
quality gates, and only an explicit human review (operator) turns it into an *approved* example.

    feedback (validated corrections) --propose--> proposed | needs_review --review--> approved | rejected

Nothing here touches ocr_results (the machine output stays immutable) or any model.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.ids import new_id
from app.core.time import utcnow
from app.extraction.schema import SCHEMA_VERSION, InvoiceData
from app.feedback.validation import _ITEM_RE, _SCALAR
from app.ml.quality import QualityConfig, blocks_approval, check_example, content_hash, group_key_for
from app.models import Client, Feedback, OcrResult, Process, Review, TrainingDataset, TrainingExample
from app.models.ml import DatasetExample
from app.observability import metrics as m
from app.services.audit import AuditService
from app.storage.base import ObjectStorage

KEY_FIELDS = ("vendor", "invoice_number", "date", "total")
TRAINABLE_POLICIES = ("tenant_only", "pooled")


def _apply(truth: dict, field: str, value) -> None:
    mi = _ITEM_RE.match(field)
    if mi:
        idx, sub = int(mi[1]), mi[2]
        items = truth.setdefault("items", [])
        while len(items) <= idx:
            items.append({})
        items[idx][sub] = value
    else:
        truth[field] = value


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class ExampleService:
    def __init__(self, db: Session, settings: Settings | None, quality: QualityConfig = QualityConfig()):
        self.db, self.settings, self.quality = db, settings, quality
        self.audit = AuditService(db)

    # ------------------------------------------------------------------ proposal
    def _client(self, client_id: str) -> Client:
        c = self.db.get(Client, client_id)
        if c is None:
            raise NotFound("Client not found", code="client_not_found")
        return c

    def propose(self, client_id: str, process_id: str, *, annotator: str | None = None,
                reviewed_truth: dict | None = None, reviewer: str | None = None) -> TrainingExample:
        """Build a proposed example from the tenant's validated corrections (or from a full reviewer-supplied truth).
        Raises Conflict when the tenant has not consented or has nothing to propose from."""
        client = self._client(client_id)
        if client.training_policy not in TRAINABLE_POLICIES:
            raise Conflict("Tenant training policy does not permit using this document", code="training_not_permitted")
        proc = self.db.execute(select(Process).where(Process.id == process_id, Process.client_id == client_id)).scalar_one_or_none()
        if proc is None:
            raise NotFound("Process not found", code="process_not_found")
        res = self.db.execute(select(OcrResult).where(OcrResult.process_id == process_id, OcrResult.client_id == client_id)).scalar_one_or_none()
        if res is None or res.data is None:
            raise Conflict("Result is no longer available", code="result_unavailable")

        today_count = self.db.execute(select(func.count()).select_from(TrainingExample).where(
            TrainingExample.client_id == client_id, TrainingExample.created_at >= utcnow() - timedelta(days=1))).scalar_one()
        existing = self.db.execute(select(TrainingExample).where(
            TrainingExample.client_id == client_id, TrainingExample.process_id == process_id)).scalar_one_or_none()
        if existing is None and today_count >= self.settings.ml_max_examples_per_tenant_per_day:
            m.ML_EXAMPLES.labels(outcome="rate_limited").inc()
            raise Conflict("Daily example contribution limit reached for this tenant", code="contribution_limit")

        machine = res.data
        truth = copy.deepcopy(machine)
        conflicts: list[str] = []
        changed: set[str] = set()
        source = "reviewer" if reviewed_truth is not None else "client_correction"
        if reviewed_truth is not None:
            truth, source = copy.deepcopy(reviewed_truth), "reviewer"
        else:
            rows = self.db.execute(select(Feedback).where(
                Feedback.client_id == client_id, Feedback.process_id == process_id,
                Feedback.status.in_(("validated", "exported")))).scalars().all()
            if not rows:
                # A person who reviewed the whole document and changed nothing has confirmed the machine result. That is a
                # human decision, not "a machine result alone", so it may be proposed (it still passes the quality gates and
                # operator review like any other example).
                confirmed = self.db.execute(select(Review.id).where(
                    Review.client_id == client_id, Review.process_id == process_id)).first() is not None
                if not confirmed:
                    raise Conflict("No validated corrections exist; a machine result alone is not ground truth",
                                   code="no_corrections")
                source = "client_confirmation"
            by_field: dict[str, set[str]] = {}
            for r in rows:
                by_field.setdefault(r.field_path, set()).add(json.dumps(r.corrected_value, sort_keys=True))
            latest = {r.field_path: r for r in sorted(rows, key=lambda r: (r.created_at, r.id))}
            for fld, vals in by_field.items():
                if len(vals) > 1:
                    conflicts.append(fld)       # contradictory annotations: leave the machine value, flag for review
                else:
                    _apply(truth, fld, latest[fld].corrected_value)
                    changed.add(fld)
        try:
            InvoiceData.model_validate(truth)
        except Exception as exc:
            raise ValidationFailed("Corrected result does not match the extraction schema", code="invalid_truth") from exc

        c_hash = content_hash(truth)
        dup = self.db.execute(select(TrainingExample.id).where(
            TrainingExample.client_id == client_id, TrainingExample.content_sha256 == c_hash,
            TrainingExample.process_id != process_id).limit(1)).scalar_one_or_none()
        pages = (res.meta or {}).get("pages") or []
        issues = check_example(
            truth, schema_version=res.schema_version if res.schema_version else SCHEMA_VERSION,
            ocr_chars=None, pages_total=len(pages) or None,
            pages_failed=sum(1 for p in pages if p.get("status") != "ok"), conflicting_fields=conflicts,
            duplicate_of=dup, cfg=self.quality)
        issues += self._poisoning_checks(client_id, annotator, machine, truth, changed)

        status = "needs_review" if issues else "proposed"
        gk = group_key_for(truth, tenant_id=client_id, batch_id=proc.batch_id)
        if existing is None:
            ex = TrainingExample(
                id=new_id("example"), client_id=client_id, process_id=process_id, schema_version=SCHEMA_VERSION, truth=truth,
                source=source, status=status, revision=1, quality_report={"issues": issues}, machine_result_sha256=_sha(machine),
                content_sha256=c_hash, group_key=gk, batch_id=proc.batch_id, annotator=annotator, reviewed_by=reviewer,
                policy_at_creation=client.training_policy, ocr_model_version_id=res.ocr_model_version_id,
                extractor_model_version_id=res.extractor_model_version_id)
            self.db.add(ex)
        else:
            ex = existing
            ex.truth, ex.status, ex.revision = truth, status, ex.revision + 1   # any change invalidates a previous approval
            ex.quality_report, ex.content_sha256, ex.group_key = {"issues": issues}, c_hash, gk
            ex.machine_result_sha256, ex.annotator, ex.source = _sha(machine), annotator, source
            ex.reviewed_by = ex.reviewed_at = None
            ex.policy_at_creation = client.training_policy
        self.audit.record(action="example.propose", resource_type="example", resource_id=ex.id, actor="admin", client_id=client_id,
                          details={"status": status, "issues": [i["code"] for i in issues], "revision": ex.revision})
        self.db.flush()
        m.ML_EXAMPLES.labels(outcome=status).inc()
        return ex

    def _poisoning_checks(self, client_id: str, api_key_id: str | None, machine: dict, truth: dict, changed: set[str]) -> list[dict]:
        """Heuristics that route suspicious contributions to a human. They never auto-reject and never auto-approve."""
        out: list[dict] = []
        key_changed = [f for f in changed if f in KEY_FIELDS]
        if len(changed) >= 3 and len(key_changed) / len(KEY_FIELDS) >= self.settings.ml_anomaly_change_fraction:
            out.append({"code": "anomalous_correction_rate", "severity": "warning", "field": None,
                        "message": "most key fields were changed in a single submission"})
        try:
            a, b = float(machine.get("total")), float(truth.get("total"))
            if a > 0 and (b / a > 100 or b / a < 0.01):
                out.append({"code": "anomalous_total_change", "severity": "warning", "field": "total",
                            "message": "total changed by more than 100x"})
        except (TypeError, ValueError):
            pass
        if api_key_id:
            n = self.db.execute(select(func.count()).select_from(Feedback).where(
                Feedback.client_id == client_id, Feedback.api_key_id == api_key_id,
                Feedback.created_at >= utcnow() - timedelta(hours=1))).scalar_one()
            if n > 500:
                out.append({"code": "burst_from_single_key", "severity": "warning", "field": None,
                            "message": "unusually many corrections from one API key in the last hour"})
        return out

    # ------------------------------------------------------------------ review
    def _example(self, example_id: str) -> TrainingExample:
        ex = self.db.get(TrainingExample, example_id)
        if ex is None:
            raise NotFound("Example not found", code="example_not_found")
        return ex

    def review(self, example_id: str, decision: str, *, reviewer: str, note: str | None = None) -> TrainingExample:
        if decision not in ("approve", "reject"):
            raise ValidationFailed("decision must be approve or reject", code="invalid_decision")
        ex = self._example(example_id)
        if ex.status not in ("proposed", "needs_review"):
            raise Conflict(f"Example is {ex.status}", code="invalid_example_state")
        client = self._client(ex.client_id)
        if decision == "approve":
            if client.training_policy not in TRAINABLE_POLICIES:
                raise Conflict("Tenant training policy no longer permits this example", code="training_not_permitted")
            if blocks_approval((ex.quality_report or {}).get("issues", [])):
                raise Conflict("Example has blocking quality issues; correct the source and re-propose", code="quality_blocked")
            ex.status = "approved"
        else:
            ex.status = "rejected"
        ex.reviewed_by, ex.reviewed_at = reviewer, utcnow()
        self.audit.record(action=f"example.{ex.status}", resource_type="example", resource_id=ex.id, actor="admin",
                          client_id=ex.client_id, details={"reviewer": reviewer, "note": (note or "")[:200]})
        m.ML_EXAMPLES.labels(outcome=ex.status).inc()
        return ex

    # ------------------------------------------------------------------ erasure / consent withdrawal
    def erase(self, client_id: str, *, process_id: str | None = None, reason: str = "erasure") -> int:
        """Delete examples (all of a tenant's, or one process's). Datasets already built from them are immutable snapshots:
        they are FLAGGED (`flagged_withdrawal`) so they are not used for new training and get rebuilt without the data."""
        q = select(TrainingExample.id).where(TrainingExample.client_id == client_id)
        if process_id:
            q = q.where(TrainingExample.process_id == process_id)
        ids = list(self.db.execute(q).scalars())
        if not ids:
            return 0
        ds_ids = set(self.db.execute(select(DatasetExample.dataset_id).where(DatasetExample.example_id.in_(ids))).scalars())
        if ds_ids:
            self.db.execute(update(TrainingDataset).where(TrainingDataset.id.in_(ds_ids)).values(status="flagged_withdrawal"))
        self.db.execute(delete(TrainingExample).where(TrainingExample.id.in_(ids)))
        self.db.flush()
        self.audit.record(action="example.erase", resource_type="client", resource_id=client_id, actor="admin", client_id=client_id,
                          details={"count": len(ids), "datasets_flagged": len(ds_ids), "reason": reason[:100]})
        return len(ids)

    def set_policy(self, client_id: str, policy: str, *, by: str) -> Client:
        if policy not in ("none", "tenant_only", "pooled"):
            raise ValidationFailed("unknown training policy", code="invalid_policy")
        c = self._client(client_id)
        old = c.training_policy
        c.training_policy, c.training_opt_in = policy, policy == "pooled"
        c.training_policy_updated_at, c.training_policy_updated_by = utcnow(), by
        self.audit.record(action="client.training_policy", resource_type="client", resource_id=c.id, actor="admin", client_id=c.id,
                          details={"from": old, "to": policy, "by": by})
        if old == "pooled" and policy != "pooled":      # pooled datasets containing this tenant must not be used further
            pooled = select(DatasetExample.dataset_id).join(TrainingExample, TrainingExample.id == DatasetExample.example_id).where(
                TrainingExample.client_id == client_id)
            self.db.execute(update(TrainingDataset).where(TrainingDataset.id.in_(pooled), TrainingDataset.scope == "pooled")
                            .values(status="flagged_withdrawal"))
        if policy == "none":     # consent withdrawn: un-approved examples go; approved ones are erased too (datasets flagged)
            self.erase(client_id, reason="consent_withdrawn")
        return c
