"""Prompt 3: example lifecycle, consent, datasets, leakage, registry gates. Uses the real DB fixture (PostgreSQL when configured)."""
import hashlib

import pytest

from app.core.errors import Conflict
from app.core.ids import new_id
from app.extraction.layout_rules import LayoutRulesExtractor
from app.extraction.rules import RuleBasedExtractor
from app.ml.dataset import build_dataset, load_dataset
from app.ml.examples import ExampleService
from app.ml.registry import ModelRegistry, PromotionGates, evaluate_gates
from app.ml.splits import SplitSpec
from app.models import DatasetExample, EvaluationRun, Feedback, ModelVersion, TrainingDataset, TrainingExample
from tests.conftest import make_tenant
from tests.helpers import submit_and_process

VENDORS = ["Alpha Traders", "Beta Stores", "Gamma Mart", "Delta Supplies", "Epsilon Pharma", "Zeta Foods", "Eta Books",
           "Theta Tools", "Iota Fabrics", "Kappa Paper", "Lambda Toys", "Mu Motors"]


def fb(client, t, pid, *corr):
    return client.post(f"/api/v1/bills/{pid}/feedback", headers=t.h,
                       json={"corrections": [{"field": f, "value": v} for f, v in corr]})


def policy(container, t, pol):
    with container.session_factory() as db:
        ExampleService(db, container.settings).set_policy(t.client_id, pol, by="test"); db.commit()


def make_example(client, container, run_queue, t, vendor, approve=True):
    pid = submit_and_process(client, t, run_queue)
    assert fb(client, t, pid, ("vendor", vendor), ("invoice_number", "INV-" + vendor.split()[0])).status_code == 201
    with container.session_factory() as db:
        svc = ExampleService(db, container.settings)
        ex = svc.propose(t.client_id, pid, annotator=t.key_id)
        if approve and ex.status == "proposed":
            svc.review(ex.id, "approve", reviewer="ops")
        db.commit()
        return pid, ex.id


# ---------------------------------------------------------------- consent & lifecycle
def test_no_consent_no_example(client, container, run_queue):
    t = make_tenant(container, "noconsent")
    pid = submit_and_process(client, t, run_queue); fb(client, t, pid, ("total", 5))
    with container.session_factory() as db:
        with pytest.raises(Conflict) as e:
            ExampleService(db, container.settings).propose(t.client_id, pid)
        assert e.value.code == "training_not_permitted"


def test_machine_result_alone_is_not_ground_truth(client, container, run_queue):
    t = make_tenant(container, "nocorr"); policy(container, t, "tenant_only")
    pid = submit_and_process(client, t, run_queue)
    with container.session_factory() as db:
        with pytest.raises(Conflict) as e:
            ExampleService(db, container.settings).propose(t.client_id, pid)
        assert e.value.code == "no_corrections"


def test_example_keeps_machine_result_immutable_and_needs_review_to_approve(client, container, run_queue):
    t = make_tenant(container, "life"); policy(container, t, "tenant_only")
    pid, eid = make_example(client, container, run_queue, t, "Alpha Traders", approve=False)
    with container.session_factory() as db:
        ex = db.get(TrainingExample, eid)
        assert ex.status in ("proposed", "needs_review") and ex.truth["vendor"] == "Alpha Traders"
        from app.models import OcrResult
        res = db.query(OcrResult).filter_by(process_id=pid).one()
        assert res.data["vendor"] != "Alpha Traders"                     # original result untouched
        assert ex.machine_result_sha256 and ex.content_sha256


def test_conflicting_corrections_are_flagged_and_block_approval(client, container, run_queue):
    t = make_tenant(container, "conflict"); policy(container, t, "tenant_only")
    pid = submit_and_process(client, t, run_queue)
    fb(client, t, pid, ("vendor", "X Ltd")); fb(client, t, pid, ("vendor", "Y Ltd"))
    with container.session_factory() as db:
        svc = ExampleService(db, container.settings)
        ex = svc.propose(t.client_id, pid)
        codes = {i["code"] for i in ex.quality_report["issues"]}
        assert ex.status == "needs_review" and "contradictory_annotation" in codes
        with pytest.raises(Conflict) as e:
            svc.review(ex.id, "approve", reviewer="ops")
        assert e.value.code == "quality_blocked"


def test_inconsistent_totals_in_truth_are_flagged_not_fixed(client, container, run_queue):
    t = make_tenant(container, "badtotal"); policy(container, t, "tenant_only")
    pid = submit_and_process(client, t, run_queue)
    fb(client, t, pid, ("total", 1.0))            # subtotal/tax in the stored result no longer add up
    with container.session_factory() as db:
        ex = ExampleService(db, container.settings).propose(t.client_id, pid)
        assert any(i["code"] == "validation:totals_mismatch" for i in ex.quality_report["issues"])
        assert ex.truth["total"] == "1.00"        # stored exactly as submitted: never silently repaired


def test_consent_withdrawal_erases_examples_and_flags_datasets(client, container, run_queue):
    t = make_tenant(container, "withdraw"); policy(container, t, "tenant_only")
    make_example(client, container, run_queue, t, "Alpha Traders")
    with container.session_factory() as db:
        ds = build_dataset(db, container.storage, container.settings, scope=f"tenant:{t.client_id}")
        ds_id = ds.id; db.commit()
    policy(container, t, "none")
    with container.session_factory() as db:
        assert db.query(TrainingExample).filter_by(client_id=t.client_id).count() == 0
        assert db.get(TrainingDataset, ds_id).status == "flagged_withdrawal"
        assert db.query(DatasetExample).filter_by(dataset_id=ds_id, example_id=None).count() == 1   # tombstone


def test_process_delete_keeps_example_only_for_pooled_consent(client, container, run_queue):
    """Same consent rule as feedback: deleting a document removes derived training data unless the tenant is pooled."""
    tp = make_tenant(container, "delp"); to = make_tenant(container, "delo")
    policy(container, tp, "pooled"); policy(container, to, "tenant_only")
    p1, _ = make_example(client, container, run_queue, tp, "Alpha Traders")
    p2, _ = make_example(client, container, run_queue, to, "Beta Stores")
    client.delete(f"/api/v1/bills/{p1}", headers=tp.h); client.delete(f"/api/v1/bills/{p2}", headers=to.h)
    with container.session_factory() as db:
        assert db.query(TrainingExample).filter_by(client_id=tp.client_id).count() == 1
        assert db.query(TrainingExample).filter_by(client_id=to.client_id).count() == 0


# ---------------------------------------------------------------- datasets
def test_dataset_tenant_isolation_and_pooled_consent(client, container, run_queue):
    a = make_tenant(container, "ta"); b = make_tenant(container, "tb"); c = make_tenant(container, "tc")
    policy(container, a, "pooled"); policy(container, b, "tenant_only"); policy(container, c, "none")
    make_example(client, container, run_queue, a, "Alpha Traders")
    make_example(client, container, run_queue, b, "Beta Stores")
    with container.session_factory() as db:
        pooled = build_dataset(db, container.storage, container.settings, scope="pooled")
        rows = load_dataset(container.storage, pooled)
        assert pooled.record_count == 1 and {r["truth"]["vendor"] for r in rows} == {"Alpha Traders"}   # tenant_only never pooled
        private = build_dataset(db, container.storage, container.settings, scope=f"tenant:{b.client_id}")
        assert [r["truth"]["vendor"] for r in load_dataset(container.storage, private)] == ["Beta Stores"]
        blob = container.storage.get(pooled.artifact_uri).decode()
        assert a.client_id not in blob and "ocr" not in blob.lower().replace("ocr_model", "")      # pseudonymous, no OCR text
        assert build_dataset(db, container.storage, container.settings, scope=f"tenant:{c.client_id}") is None   # no consent


def test_dataset_artifact_tamper_is_detected(client, container, run_queue):
    t = make_tenant(container, "tamper"); policy(container, t, "tenant_only")
    make_example(client, container, run_queue, t, "Alpha Traders")
    with container.session_factory() as db:
        ds = build_dataset(db, container.storage, container.settings, scope=f"tenant:{t.client_id}")
        container.storage.put(ds.artifact_uri, b'{"tampered":true}\n', "application/x-ndjson")
        with pytest.raises(Conflict) as e:
            load_dataset(container.storage, ds)
        assert e.value.code == "dataset_corrupt"


def test_training_and_evaluation_sets_do_not_share_groups(client, container, run_queue):
    t = make_tenant(container, "leak"); policy(container, t, "tenant_only")
    for i in range(12):
        make_example(client, container, run_queue, t, VENDORS[i])
    scope = f"tenant:{t.client_id}"
    with container.session_factory() as db:
        # Tenant ids are random, so group keys differ per run. Choose the first seed under which these 12 groups yield at
        # least one test group and one train group (deterministic for the given groups); otherwise the test is flaky
        # (about 1 run in 7 had no test-split group with the fixed seed "s").
        from app.ml.splits import assign
        gks = {r.group_key for r in db.query(TrainingExample).filter_by(client_id=t.client_id)}
        seed = next(str(i) for i in range(500)
                    if {"train", "test"} <= {assign(g, SplitSpec(seed=str(i))) for g in gks})
        ev = build_dataset(db, container.storage, container.settings, scope=scope, kind="evaluation",
                           spec=SplitSpec(seed=seed, min_groups_per_eval_split=1), allow_small_eval=True)
        tr = build_dataset(db, container.storage, container.settings, scope=scope, kind="training", spec=SplitSpec(seed=seed))
        assert ev is not None and tr is not None
        eg = {r.group_key for r in db.query(DatasetExample).filter_by(dataset_id=ev.id)}
        tg = {r.group_key for r in db.query(DatasetExample).filter_by(dataset_id=tr.id)}
        assert eg and tg and not (eg & tg)
        assert {r["split"] for r in load_dataset(container.storage, ev)} == {"test"}
        assert "test" not in {r["split"] for r in load_dataset(container.storage, tr)}


def test_small_evaluation_set_is_refused_with_reason(client, container, run_queue):
    t = make_tenant(container, "small"); policy(container, t, "tenant_only")
    make_example(client, container, run_queue, t, "Alpha Traders")
    with container.session_factory() as db:
        # 1 example cannot give a meaningful evaluation; the builder refuses instead of producing a misleading number
        assert build_dataset(db, container.storage, container.settings, scope=f"tenant:{t.client_id}", kind="evaluation",
                             spec=SplitSpec(seed="s", train=0.0, val=0.0, test=1.0)) is None


# ---------------------------------------------------------------- registry
def _run(db, mv, *, dsha="d1", n=500, synthetic=False, **metrics):
    r = EvaluationRun(id=new_id("eval"), model_version_id=mv.id, dataset_ref="test", dataset_sha256=dsha, split="test",
                      sample_count=n, metrics=metrics, slices={}, config={}, environment={}, synthetic=synthetic,
                      result_sha256="0" * 64)
    db.add(r); db.flush()
    return r


GATES = PromotionGates(min_samples=100)


def test_gates_block_synthetic_small_regressing_and_unbaselined(container):
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        ref = reg.register_candidate(kind="extractor", name="ref", version="0", artifact_uri=None, dataset_id=None)
        rref = _run(db, ref, field_f1=0.80, total_exact=0.9, latency_p95_ms=100)
        mv = reg.register_candidate(kind="extractor", name="m", version="1", artifact_uri=None, dataset_id=None)
        cases = {
            "synthetic_evidence_not_allowed": _run(db, mv, synthetic=True, field_f1=0.9),
            "insufficient_samples": _run(db, mv, n=10, field_f1=0.9),
            "regression": _run(db, mv, field_f1=0.9, total_exact=0.5),
            "different_evaluation_dataset": _run(db, mv, dsha="other", field_f1=0.9),
            "no_improvement": _run(db, mv, field_f1=0.80, total_exact=0.9),
            "latency_regression": _run(db, mv, field_f1=0.9, total_exact=0.95, latency_p95_ms=500),
        }
        for code, run in cases.items():
            res = evaluate_gates(run, rref, GATES)
            assert not res.passed and code in {f["code"] for f in res.failures}, (code, res.failures)
        assert "no_reference_evaluation" in {f["code"] for f in evaluate_gates(cases["regression"], None, GATES).failures}
        good = _run(db, mv, field_f1=0.9, total_exact=0.95, latency_p95_ms=110)
        assert evaluate_gates(good, rref, GATES).passed


def test_promotion_requires_gates_then_rollback_a_b_a(container):
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        a = reg.register_candidate(kind="extractor", name="m", version="A", artifact_uri=None, dataset_id=None)
        ref = reg.register_candidate(kind="extractor", name="ref", version="0", artifact_uri=None, dataset_id=None)
        rref = _run(db, ref, field_f1=0.70)
        ra = _run(db, a, field_f1=0.80); reg.record_evaluation(a.id, run=ra)
        with pytest.raises(Conflict):
            reg.promote(a.id, promoted_by="ops")                                   # evaluated but not approved
        assert not reg.approve_with_gates(a.id, GATES, reference_run_id=None).passed   # no reference at all
        assert reg.approve_with_gates(a.id, GATES, reference_run_id=rref.id).passed
        reg.promote(a.id, promoted_by="ops")
        b = reg.register_candidate(kind="extractor", name="m", version="B", artifact_uri=None, dataset_id=None)
        bad = _run(db, b, field_f1=0.75); reg.record_evaluation(b.id, run=bad)
        res = reg.approve_with_gates(b.id, GATES, reject_on_failure=True)          # compared with production A on same data
        assert not res.passed and db.get(ModelVersion, b.id).status == "rejected"
        assert reg.production("extractor").id == a.id
        b2 = reg.register_candidate(kind="extractor", name="m", version="B2", artifact_uri=None, dataset_id=None)
        reg.record_evaluation(b2.id, run=_run(db, b2, field_f1=0.85))
        assert reg.approve_with_gates(b2.id, GATES).passed
        reg.promote(b2.id, promoted_by="ops")
        assert reg.production("extractor").id == b2.id and db.get(ModelVersion, a.id).status == "retired"
        reg.rollback("extractor", a.id, by="ops")                                  # A -> B -> A
        assert reg.production("extractor").id == a.id and db.get(ModelVersion, b2.id).status == "retired"
        with pytest.raises(Conflict):
            reg.rollback("extractor", b.id, by="ops")                              # never in production
        db.commit()


def test_evaluation_runs_are_immutable(container):
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        a = reg.register_candidate(kind="extractor", name="m", version="I", artifact_uri=None, dataset_id=None)
        r = _run(db, a, field_f1=0.5); db.commit()
        r.metrics = {"field_f1": 0.99}
        with pytest.raises(Exception):
            db.commit()
        db.rollback()
        db.delete(db.get(EvaluationRun, r.id))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


# ---------------------------------------------------------------- evaluation harness -> immutable history -> gates
def _eval_docs():
    from PIL import Image
    from app.ml.evaluation import EvalDoc
    from tests.unit.test_pipeline import ROW_LINES
    truth = {"vendor": "Himal Stationers", "invoice_number": "INV-2026-0042", "date": "2026-03-15", "total": "4746.00",
             "subtotal": "4200.00", "tax": "546.00",
             "items": [{"description": "Rice 25kg", "quantity": "2", "unit_price": "1500.00", "amount": "3000.00"},
                       {"description": "Cooking Oil", "quantity": "1", "unit_price": "1200.00", "amount": "1200.00"}]}
    text = "\n".join(l.text for l in ROW_LINES)
    return [EvalDoc(f"d{i}", [Image.new("RGB", (10, 10))], truth, text, {"template": "std"}) for i in range(3)]


def test_harness_measures_persists_immutably_and_gates_refuse_tiny_synthetic_runs(container):
    from app.ml.evaluation import ReplayOCR, evaluate, persist_run
    from app.ocr.base import OCRProvider
    from tests.unit.test_pipeline import ROW_LINES, ocr_result

    class Inner(OCRProvider):
        name, version = "inner", "1"
    replay = ReplayOCR([ocr_result([ROW_LINES])] * 3, Inner())
    out = evaluate(_eval_docs(), replay, RuleBasedExtractor())
    m = out.metrics
    assert out.sample_count == 3 and m["failure_rate"] == 0 and m["cer"] == 0 and m["wer"] == 0
    assert m["total_exact"] == 1.0 and m["date_exact"] == 1.0 and m["line_item_f1"] == 1.0
    assert m["critical_field_f1"] is not None and m["latency_p95_ms"] is not None and out.slices["template=std"]["n"] == 3
    assert out.environment["harness_version"] and out.dataset_sha256 == evaluate(_eval_docs(), ReplayOCR([ocr_result([ROW_LINES])] * 3, Inner()),
                                                                               RuleBasedExtractor()).dataset_sha256

    with container.session_factory() as db:
        reg = ModelRegistry(db)
        ref = reg.register_candidate(kind="extractor", name="rule-based-baseline", version="0.1.0", artifact_uri=None, dataset_id=None)
        cand = reg.register_candidate(kind="extractor", name="layout-rules", version="0.1.0", artifact_uri=None, dataset_id=None)
        rref = persist_run(db, ref.id, out, dataset_ref="synthetic:test", synthetic=True)
        out2 = evaluate(_eval_docs(), ReplayOCR([ocr_result([ROW_LINES])] * 3, Inner()), LayoutRulesExtractor())
        rc = persist_run(db, cand.id, out2, dataset_ref="synthetic:test", synthetic=True)
        db.commit()
        assert rc.result_sha256 and rc.synthetic and rc.sample_count == 3
        reg.record_evaluation(cand.id, run=rc)
        res = reg.approve_with_gates(cand.id, PromotionGates(min_samples=1), reference_run_id=rref.id)
        assert not res.passed and "synthetic_evidence_not_allowed" in {f["code"] for f in res.failures}
        res = reg.approve_with_gates(cand.id, PromotionGates(), reference_run_id=rref.id)
        assert {"synthetic_evidence_not_allowed", "insufficient_samples"} <= {f["code"] for f in res.failures}
        assert db.get(ModelVersion, cand.id).status == "evaluated"                   # stays evaluated; never promoted on this evidence
        rc.metrics = {}
        with pytest.raises(Exception):
            db.commit()
        db.rollback()


def test_harness_counts_extractor_failures_against_the_candidate():
    from app.ml.evaluation import ReplayOCR, evaluate
    from app.extraction.base import Extractor
    from app.ocr.base import OCRProvider
    from tests.unit.test_pipeline import ROW_LINES, ocr_result

    class Inner(OCRProvider):
        name, version = "inner", "1"

    class Boom(Extractor):
        name, version = "boom", "1"
        def extract(self, ocr, pages=None): raise RuntimeError("x")
    out = evaluate(_eval_docs(), ReplayOCR([ocr_result([ROW_LINES])] * 3, Inner()), Boom())
    assert out.metrics["failure_rate"] == 1.0 and all(v == 0.0 for v in out.per_document.values())
