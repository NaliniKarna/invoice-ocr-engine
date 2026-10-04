from datetime import timedelta

import pytest

from app.core.errors import Conflict
from app.core.time import utcnow
from app.models import Feedback, ModelVersion, OcrResult, Process
from app.ml.dataset import build_dataset
from app.ml.registry import ModelRegistry
from app.ocr.base import OCRPermanentError, OCRTransientError
from app.workers.job import Outcome, run_job
from app.workers.maintenance import purge_expired, reap_stale
from tests.conftest import make_tenant
from tests.helpers import submit_and_process, upload


# ---------------------------------------------------------------- worker
def test_transient_failure_retries_then_succeeds(client, tenant_a, ocr, queue, worker_ctx):
    pid = upload(client, tenant_a).json()["process_id"]
    ocr.fail_with = OCRTransientError("blip")
    r1 = run_job(queue.items.pop(), worker_ctx)
    assert r1.outcome is Outcome.RETRY and r1.countdown > 0
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["status"] == "queued"
    ocr.fail_with = None
    assert run_job(pid, worker_ctx).outcome is Outcome.COMPLETED
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["status"] == "completed" and body["attempts"] == 2


def test_retry_budget_exhausted_marks_failed(client, tenant_a, ocr, queue, worker_ctx):
    pid = upload(client, tenant_a).json()["process_id"]
    ocr.fail_with = OCRTransientError("down")
    outcomes = [run_job(pid, worker_ctx).outcome for _ in range(3)]
    assert outcomes == [Outcome.RETRY, Outcome.RETRY, Outcome.FAILED]
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["status"] == "failed" and body["error"]["code"] == "ocr_transient" and body["error"]["retryable"] is False


def test_permanent_failure_is_immediate_and_message_is_safe(client, tenant_a, ocr, queue, worker_ctx):
    pid = upload(client, tenant_a).json()["process_id"]
    ocr.fail_with = OCRPermanentError("/srv/secret/path exploded: password=hunter2")
    assert run_job(pid, worker_ctx).outcome is Outcome.FAILED
    text = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).text
    assert "hunter2" not in text and "/srv" not in text


def test_unexpected_exception_is_contained(client, tenant_a, ocr, worker_ctx):
    pid = upload(client, tenant_a).json()["process_id"]
    ocr.fail_with = RuntimeError("boom")
    res = [run_job(pid, worker_ctx).outcome for _ in range(3)]
    assert res[-1] is Outcome.FAILED
    assert "boom" not in client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).text


def test_duplicate_delivery_is_harmless(client, tenant_a, run_queue, worker_ctx, ocr):
    pid = submit_and_process(client, tenant_a, run_queue)
    calls = ocr.calls
    assert run_job(pid, worker_ctx).outcome is Outcome.SKIPPED and ocr.calls == calls


def test_result_records_model_versions(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    with container.session_factory() as db:
        r = db.query(OcrResult).filter_by(process_id=pid).one()
        assert db.get(ModelVersion, r.ocr_model_version_id).name == "fake-ocr"
        assert db.get(ModelVersion, r.extractor_model_version_id).version == "0.1.0"
        assert r.total_ms >= 0 and r.page_count == 1


def test_unconfigured_ocr_fails_loudly_not_fake_data(client, tenant_a, container, queue):
    from app.extraction.rules import RuleBasedExtractor
    from app.ocr.providers import UnconfiguredProvider
    from app.workers.job import WorkerContext
    ctx = WorkerContext(container.settings, container.session_factory, container.storage, UnconfiguredProvider(), RuleBasedExtractor())
    pid = upload(client, tenant_a).json()["process_id"]
    assert run_job(pid, ctx).outcome is Outcome.FAILED
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["error"]["code"] == "ocr_not_configured"


# ---------------------------------------------------------------- reaper / retention
def test_reaper_enqueues_orphaned_queued_jobs(client, tenant_a, queue, worker_ctx, container):
    queue.fail = True
    pid = upload(client, tenant_a).json()["process_id"]
    queue.fail = False
    with container.session_factory() as db:
        db.query(Process).update({"created_at": utcnow() - timedelta(hours=1)}); db.commit()
    assert reap_stale(worker_ctx, queue)["requeued"] == 1 and queue.items == [pid]


def test_reaper_recovers_crashed_worker_jobs(client, tenant_a, queue, worker_ctx, container):
    pid = upload(client, tenant_a).json()["process_id"]; queue.items.clear()
    with container.session_factory() as db:
        db.query(Process).update({"status": "processing", "attempts": 1, "started_at": utcnow() - timedelta(hours=1)}); db.commit()
    assert reap_stale(worker_ctx, queue)["recovered"] == 1 and queue.items == [pid]


def test_reaper_fails_jobs_out_of_attempts(client, tenant_a, queue, worker_ctx, container):
    pid = upload(client, tenant_a).json()["process_id"]; queue.items.clear()
    with container.session_factory() as db:
        db.query(Process).update({"status": "processing", "attempts": 3, "started_at": utcnow() - timedelta(hours=1)}); db.commit()
    assert reap_stale(worker_ctx, queue)["failed"] == 1
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["error"]["code"] == "worker_timeout"


def test_retention_purge(client, tenant_a, run_queue, worker_ctx, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    with container.session_factory() as db:
        key = db.get(Process, pid).storage_key
        db.query(Process).update({"expires_at": utcnow() - timedelta(days=1)}); db.commit()
    assert purge_expired(worker_ctx) == 1
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code == 404
    with pytest.raises(Exception):
        container.storage.get(key)


# ---------------------------------------------------------------- feedback
def fb(client, t, pid, *corr, comment=None):
    return client.post(f"/api/v1/bills/{pid}/feedback", headers=t.h,
                       json={"corrections": [{"field": f, "value": v} for f, v in corr], "comment": comment})


def test_feedback_stores_original_corrected_and_model_versions(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = fb(client, tenant_a, pid, ("total", 4800.5), ("vendor", "Himalayan Traders"))
    assert r.status_code == 201 and r.json()["accepted"] == 2
    with container.session_factory() as db:
        rows = {f.field_path: f for f in db.query(Feedback).all()}
        t = rows["total"]
        assert (t.original_value, t.corrected_value, t.client_id, t.process_id) == ("4746.00", "4800.50", tenant_a.client_id, pid)
        assert t.ocr_model_version_id and t.extractor_model_version_id and t.created_at and t.status == "validated"
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["result"]["total"] == 4746.0                   # original result untouched


def test_feedback_requires_completed(client, tenant_a):
    pid = upload(client, tenant_a).json()["process_id"]
    assert fb(client, tenant_a, pid, ("total", 1)).status_code == 409


def test_feedback_is_atomic_on_invalid_input(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    r = fb(client, tenant_a, pid, ("total", 1), ("bogus", 2))
    assert r.status_code == 422 and r.json()["error"]["details"][0]["code"] == "unknown_field"
    with container.session_factory() as db:
        assert db.query(Feedback).count() == 0


def test_feedback_size_limits(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    assert client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": []}).status_code == 422
    many = [{"field": "total", "value": i} for i in range(101)]
    assert client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": many}).status_code == 422


def test_delete_removes_feedback_unless_opted_in(client, container, run_queue):
    out = make_tenant(container, "noopt"); inn = make_tenant(container, "optin", opt_in=True)
    p1 = submit_and_process(client, out, run_queue); p2 = submit_and_process(client, inn, run_queue)
    fb(client, out, p1, ("total", 1)); fb(client, inn, p2, ("total", 1))
    client.delete(f"/api/v1/bills/{p1}", headers=out.h); client.delete(f"/api/v1/bills/{p2}", headers=inn.h)
    with container.session_factory() as db:
        assert [f.client_id for f in db.query(Feedback).all()] == [inn.client_id]


# ---------------------------------------------------------------- ML pipeline
def test_raw_feedback_alone_never_becomes_a_dataset(client, container, run_queue):
    """Prompt 3 replaces the Prompt 2 rule (opted-in feedback exports directly) with: feedback -> reviewed example ->
    dataset. Corrections are not ground truth, so feedback by itself must produce no dataset. The positive path
    (approved examples of consenting tenants, conflicts flagged) is covered in test_ml_pipeline.py."""
    opt = make_tenant(container, "opt", opt_in=True)
    a = submit_and_process(client, opt, run_queue)
    fb(client, opt, a, ("total", 10))
    with container.session_factory() as db:
        assert build_dataset(db, container.storage, container.settings, scope="pooled") is None
        assert db.query(Feedback).filter_by(status="validated").count() == 1      # untouched


def test_feedback_never_changes_active_model(client, tenant_a, run_queue, container):
    pid = submit_and_process(client, tenant_a, run_queue)
    fb(client, tenant_a, pid, ("total", 1))
    with container.session_factory() as db:
        assert {m.status for m in db.query(ModelVersion)} == {"candidate"}     # nothing promoted by feedback


def test_model_registry_gated_promotion(container):
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        m1 = reg.register_candidate(kind="extractor", name="layout", version="1", artifact_uri="s3://m/1", dataset_id=None)
        with pytest.raises(Conflict):
            reg.promote(m1.id, promoted_by="ops")                        # cannot skip evaluation
        reg.record_evaluation(m1.id, {"field_f1": 0.91})
        reg.approve(m1.id, baseline_metric=0.85); reg.promote(m1.id, promoted_by="ops")
        m2 = reg.register_candidate(kind="extractor", name="layout", version="2", artifact_uri="s3://m/2", dataset_id=None, parent_id=m1.id)
        reg.record_evaluation(m2.id, {"field_f1": 0.80})
        with pytest.raises(Conflict):
            reg.approve(m2.id, baseline_metric=0.91)                     # worse than baseline -> rejected
        assert db.get(ModelVersion, m2.id).status == "rejected"
        m3 = reg.register_candidate(kind="extractor", name="layout", version="3", artifact_uri="s3://m/3", dataset_id=None)
        reg.record_evaluation(m3.id, {"field_f1": 0.95}); reg.approve(m3.id, baseline_metric=0.91); reg.promote(m3.id, promoted_by="ops")
        db.commit()
        active = db.query(ModelVersion).filter_by(kind="extractor", status="production").all()
        assert [m.version for m in active] == ["3"] and db.get(ModelVersion, m1.id).status == "retired"
        reg.rollback("extractor", m1.id, by="ops"); db.commit()
        assert [m.version for m in db.query(ModelVersion).filter_by(kind="extractor", status="production")] == ["1"]
