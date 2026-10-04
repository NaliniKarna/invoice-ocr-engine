"""Model runtime (load once, verify, fall back), operator ML API security, public API exposure, training orchestration."""
import hashlib
import json

import pytest
from pydantic import SecretStr

from app.core.errors import Conflict
from app.extraction.layout_rules import LayoutRulesExtractor
from app.extraction.rules import RuleBasedExtractor
from app.ml import artifacts
from app.ml.interfaces import TrainingRequest, TrainingResult
from app.ml.registry import ModelRegistry
from app.ml.runtime import ModelRuntime, register_extractor_factory
from app.ml.training import InsufficientTrainingData, ReadinessPolicy, run_training
from app.models import ModelVersion, TrainingDataset
from app.observability import metrics as pm
from app.workers.job import WorkerContext, run_job
from tests.conftest import make_tenant
from tests.helpers import submit_and_process, upload

KEY = "k" * 32


class JsonModelExtractor(RuleBasedExtractor):
    """Test-only stand-in for a trained model: behaves like the reference, but is identified by its artifact."""
    def __init__(self, cfg):
        super().__init__()
        self.name, self.version = "json-model", cfg.get("version", "1")


register_extractor_factory("json-model", lambda blob, cfg: JsonModelExtractor({"version": json.loads(blob)["version"]} if blob else cfg))


def _settings(container):
    return container.settings.model_copy(update={"ml_model_signing_key": SecretStr(KEY)})


def _runtime(container, settings=None):
    return ModelRuntime(settings or _settings(container), container.session_factory, container.storage, RuleBasedExtractor(),
                        refresh_seconds=0)


def _publish(container, *, name="json-model", version="1", blob=None, uri=None, sign_with=KEY, schema="2", ext=".json",
             tamper=None, production=True):
    blob = blob if blob is not None else json.dumps({"version": version}).encode()
    digest = hashlib.sha256(blob).hexdigest()
    uri = uri or f"models/{name}/{digest}{ext}"
    container.storage.put(uri, tamper if tamper is not None else blob, "application/octet-stream")
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        mv = reg.register_candidate(kind="extractor", name=name, version=version, artifact_uri=uri, dataset_id=None,
                                    schema_version=schema, artifact_sha256=digest,
                                    signature=artifacts.sign(sign_with.encode(), digest) if sign_with else None)
        if production:
            mv.status = "approved"; reg.promote(mv.id, promoted_by="test")
        db.commit()
        return mv.id


def _errs(reason):
    return pm.ML_MODEL_LOAD_ERRORS.labels(kind="extractor", reason=reason)._value.get()


# ------------------------------------------------------------------ runtime
def test_no_production_model_uses_reference(container):
    rt = _runtime(container)
    assert rt.current().name == "rule-based-baseline" and rt.status()["using_reference"]


def test_builtin_extractor_promoted_through_registry_is_loaded_once(container):
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        mv = reg.register_candidate(kind="extractor", name="layout-rules", version=LayoutRulesExtractor.version, artifact_uri=None, dataset_id=None)
        mv.status = "approved"; reg.promote(mv.id, promoted_by="t"); db.commit()
    rt = _runtime(container)
    first = rt.current()
    assert first.name == "layout-rules" and rt.current() is first and not rt.status()["using_reference"]


def test_signed_artifact_model_loads(container):
    mid = _publish(container, version="7")
    rt = _runtime(container)
    assert rt.current().name == "json-model" and rt.current().version == "7" and rt.status()["model_id"] == mid


@pytest.mark.parametrize("case,reason", [
    ("tampered", "checksum_mismatch"), ("unsigned", "bad_signature"), ("wrong_key", "bad_signature"),
    ("pickle_ext", "format_not_allowed"), ("unknown", "unknown_model_type"), ("schema", "schema_incompatible"),
])
def test_bad_model_never_serves_and_reference_takes_over(container, case, reason):
    kw = {"tampered": dict(tamper=b'{"version":"666"}'), "unsigned": dict(sign_with=None), "wrong_key": dict(sign_with="z" * 32),
          "pickle_ext": dict(ext=".pkl"), "unknown": dict(name="mystery-net"), "schema": dict(schema="99")}[case]
    _publish(container, **kw)
    before = _errs(reason)
    rt = _runtime(container)
    assert rt.current().name == "rule-based-baseline"
    assert rt.status()["using_reference"] and rt.status()["last_error"] == reason and _errs(reason) == before + 1
    rt.current(); rt.current()
    assert _errs(reason) == before + 1                                        # sticky: no retry storm on every call


def test_missing_signing_key_disables_artifact_models(container):
    _publish(container)
    rt = ModelRuntime(container.settings, container.session_factory, container.storage, RuleBasedExtractor(), refresh_seconds=0)
    assert rt.current().name == "rule-based-baseline" and rt.status()["last_error"] == "no_signing_key"


def test_missing_artifact_object_falls_back(container):
    mid = _publish(container)
    with container.session_factory() as db:
        container.storage.delete(db.get(ModelVersion, mid).artifact_uri)
    rt = _runtime(container)
    assert rt.current().name == "rule-based-baseline" and rt.status()["last_error"] == "load_error"


def test_pointer_change_and_rollback_a_b_a_at_runtime(container):
    a = _publish(container, version="A")
    rt = _runtime(container)
    assert rt.current().version == "A"
    b = _publish(container, version="B", blob=b'{"version":"B"}')
    assert rt.current().version == "B"
    with container.session_factory() as db:
        ModelRegistry(db).rollback("extractor", a, by="ops"); db.commit()
    assert rt.current().version == "A"
    with container.session_factory() as db:
        ModelRegistry(db).retire(a); db.commit()
    assert rt.current().name == "rule-based-baseline"                           # retiring production => reference extractor


def test_registry_outage_keeps_serving_current_model(container):
    _publish(container, version="S")
    rt = _runtime(container)
    assert rt.current().version == "S"
    rt.sf = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert rt.current().version == "S" and rt.status()["last_error"] == "registry_unreachable"


def test_refresh_is_rate_limited(container):
    t = [0.0]
    rt = ModelRuntime(_settings(container), container.session_factory, container.storage, RuleBasedExtractor(),
                      refresh_seconds=30, clock=lambda: t[0])
    assert rt.current().name == "rule-based-baseline"
    _publish(container)
    t[0] = 5; assert rt.current().name == "rule-based-baseline"                   # not re-read within 30s
    t[0] = 31; assert rt.current().name == "json-model"


def test_job_uses_production_model_and_records_the_extractor_that_ran(client, container, tenant_a, ocr, queue):
    _publish(container, version="9")
    ctx = WorkerContext(_settings(container), container.session_factory, container.storage, ocr, RuleBasedExtractor(),
                        ModelRuntime(_settings(container), container.session_factory, container.storage, RuleBasedExtractor(), refresh_seconds=0))
    pid = upload(client, tenant_a).json()["process_id"]
    run_job(queue.items.pop(), ctx)
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["status"] == "completed" and body["model_versions"]["extractor"] == "json-model@9"


# ------------------------------------------------------------------ public API exposure
def test_public_response_hides_internal_evidence(client, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    text = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).text.lower()
    for leak in ("bbox", "source_text", "evidence", '"meta"', "ocr_artifact", "storage_key"):
        assert leak not in text
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["pipeline_versions"]["confidence_method"] == "heuristic-v2" and body["confidence_calibrated"] is False
    assert body["pipeline_versions"]["schema_version"] == "2"


def test_evidence_is_stored_internally_and_erased_with_the_result(client, container, tenant_a, run_queue):
    from app.models import OcrResult
    pid = submit_and_process(client, tenant_a, run_queue)
    with container.session_factory() as db:
        res = db.query(OcrResult).filter_by(process_id=pid).one()
        assert res.meta["evidence"] and res.meta["pages"][0]["status"] == "ok" and res.meta["versions"]["validation"]
    assert client.delete(f"/api/v1/bills/{pid}", headers=tenant_a.h).status_code in (200, 204)
    with container.session_factory() as db:
        res = db.query(OcrResult).filter_by(process_id=pid).one()
        assert res.data is None and "evidence" not in res.meta and "pages" not in res.meta


# ------------------------------------------------------------------ operator API security
def _admin(container):
    return {"X-Admin-Token": container.settings.admin_token.get_secret_value()}


@pytest.mark.parametrize("method,path", [
    ("get", "/internal/v1/models"), ("get", "/internal/v1/examples"), ("post", "/internal/v1/datasets"),
    ("post", "/internal/v1/examples"), ("post", "/internal/v1/models/x/promote"), ("put", "/internal/v1/clients/x/training-policy"),
])
def test_tenant_api_key_cannot_use_operator_ml_endpoints(client, tenant_a, method, path):
    body = {} if method == "get" else {"json": {}}
    assert getattr(client, method)(path, headers=tenant_a.h, **body).status_code in (401, 403)
    assert getattr(client, method)(path, **body).status_code in (401, 403)


def test_operator_flow_policy_propose_review_dataset(client, container, run_queue):
    t = make_tenant(container, "flow")
    pid = submit_and_process(client, t, run_queue)
    client.post(f"/api/v1/bills/{pid}/feedback", headers=t.h, json={"corrections": [{"field": "vendor", "value": "Flow Traders"}]})
    a = _admin(container)
    assert client.post("/internal/v1/examples", headers=a, json={"client_id": t.client_id, "process_id": pid}).status_code == 409   # no consent
    assert client.put(f"/internal/v1/clients/{t.client_id}/training-policy", headers=a, json={"policy": "tenant_only"}).json()["training_policy"] == "tenant_only"
    ex = client.post("/internal/v1/examples", headers=a, json={"client_id": t.client_id, "process_id": pid})
    assert ex.status_code == 201 and ex.json()["status"] in ("proposed", "needs_review")
    eid = ex.json()["example_id"]
    assert client.get("/internal/v1/examples", headers=a, params={"client_id": t.client_id}).json()["items"][0]["example_id"] == eid
    rv = client.post(f"/internal/v1/examples/{eid}/review", headers=a, json={"decision": "approve", "reviewer": "qa1"})
    assert rv.status_code == 200 and rv.json()["status"] == "approved" and rv.json()["reviewed_by"] == "qa1"
    ds = client.post("/internal/v1/datasets", headers=a, json={"scope": f"tenant:{t.client_id}"}).json()
    assert ds["created"] and ds["records"] == 1 and ds["scope"] == f"tenant:{t.client_id}"
    assert "Flow Traders" not in json.dumps(ds)                                   # API never echoes ground truth
    bad = client.post("/internal/v1/datasets", headers=a, json={"scope": "everything"})
    assert bad.status_code == 422


def test_operator_cannot_propose_another_tenants_process(client, container, run_queue):
    a_t, b_t = make_tenant(container, "oa"), make_tenant(container, "ob")
    pid_b = submit_and_process(client, b_t, run_queue)
    client.post(f"/api/v1/bills/{pid_b}/feedback", headers=b_t.h, json={"corrections": [{"field": "vendor", "value": "B Ltd"}]})
    a = _admin(container)
    client.put(f"/internal/v1/clients/{a_t.client_id}/training-policy", headers=a, json={"policy": "tenant_only"})
    r = client.post("/internal/v1/examples", headers=a, json={"client_id": a_t.client_id, "process_id": pid_b})
    assert r.status_code == 404


def test_promotion_over_api_needs_gates_and_cannot_skip_states(client, container):
    a = _admin(container)
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        mv = reg.register_candidate(kind="extractor", name="layout-rules", version="x", artifact_uri=None, dataset_id=None)
        db.commit(); mid = mv.id
    assert client.post(f"/internal/v1/models/{mid}/promote", headers=a, json={}).status_code == 409        # candidate
    assert client.post(f"/internal/v1/models/{mid}/approve", headers=a, json={}).status_code == 409        # not evaluated
    assert client.get(f"/internal/v1/models/{mid}/evaluations", headers=a).json()["items"] == []


# ------------------------------------------------------------------ training orchestration
class StubTrainer:
    name, version = "stub-trainer", "0"

    def __init__(self):
        self.seen = None

    def train(self, req: TrainingRequest) -> TrainingResult:
        self.seen = req
        return TrainingResult(json.dumps({"version": "t1", "n": len(req.train_records)}).encode(), ".json", "json-model", "2",
                              {"train_loss": 0.1})


def _dataset_with(container, records, *, kind="training", status="active"):
    blob = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
    uri = f"datasets/test_{hashlib.sha1(blob).hexdigest()[:8]}.jsonl"
    container.storage.put(uri, blob, "application/x-ndjson")
    with container.session_factory() as db:
        ds = TrainingDataset(id="ds_" + hashlib.sha1(blob).hexdigest()[:32], artifact_uri=uri, record_count=len(records),
                             checksum_sha256=hashlib.sha256(blob).hexdigest(), kind=kind, status=status, scope="pooled")
        db.add(ds); db.commit()
        return ds.id


def _recs(n_train, n_val, test=0):
    def r(i, split): return {"example_id": f"e{i}{split}", "split": split, "group": f"g{i}", "truth": {"vendor": "v", "invoice_number": "i", "date": "2026-01-01", "total": "1"}}
    return [r(i, "train") for i in range(n_train)] + [r(i + 10_000, "val") for i in range(n_val)] + [r(i + 20_000, "test") for i in range(test)]


SMALL_OK = ReadinessPolicy(min_train_examples=5, min_train_groups=5, min_val_examples=2, min_support_per_field=5)


def test_training_refused_when_data_is_insufficient(container):
    dsid = _dataset_with(container, _recs(10, 2))
    with container.session_factory() as db:
        with pytest.raises(InsufficientTrainingData) as e:
            run_training(db, container.storage, _settings(container), StubTrainer(), db.get(TrainingDataset, dsid))    # default policy: 1000+
        assert {p["code"] for p in e.value.detail["problems"]} >= {"train_examples", "train_groups", "val_examples"}
        assert db.query(ModelVersion).count() == 0                               # nothing fabricated


def test_training_guards_flagged_test_split_wrong_kind_and_tamper(container):
    s = _settings(container)
    with container.session_factory() as db:
        for ds_id, code in ((_dataset_with(container, _recs(6, 3), status="flagged_withdrawal"), "dataset_flagged"),
                            (_dataset_with(container, _recs(6, 3, test=2)), "split_leakage"),
                            (_dataset_with(container, _recs(7, 3), kind="evaluation"), "wrong_dataset_kind")):
            with pytest.raises(Conflict) as e:
                run_training(db, container.storage, s, StubTrainer(), db.get(TrainingDataset, ds_id), policy=SMALL_OK)
            assert e.value.code == code
        ds_id = _dataset_with(container, _recs(8, 3))
        ds = db.get(TrainingDataset, ds_id)
        container.storage.put(ds.artifact_uri, b'{"split":"train"}\n', "application/x-ndjson")
        with pytest.raises(Conflict) as e:
            run_training(db, container.storage, s, StubTrainer(), ds, policy=SMALL_OK)
        assert e.value.code == "dataset_corrupt"


def test_training_produces_signed_reproducible_candidate_never_production(container):
    dsid = _dataset_with(container, _recs(8, 3))
    s = _settings(container)
    with container.session_factory() as db:
        tr = StubTrainer()
        mv = run_training(db, container.storage, s, tr, db.get(TrainingDataset, dsid), seed=42, hyperparameters={"lr": 0.1}, policy=SMALL_OK)
        db.commit()
        assert mv.status == "candidate" and mv.dataset_id == dsid and mv.schema_version == "2"
        assert mv.training_meta["seed"] == 42 and mv.training_meta["dataset_sha256"] and mv.training_meta["hyperparameters"] == {"lr": 0.1}
        tm = mv.training_meta
        assert {"app_version", "code_sha256", "libraries", "schema_version", "normalization_version", "preprocessing_version",
                "dataset_split_spec", "dataset_scope", "base_model_id", "trainer", "python"} <= set(tm)
        assert len(tm["code_sha256"]) == 64 and tm["preprocessing_version"] == "1"     # enough to trace the model to its inputs
        assert "test" not in {r["split"] for r in tr.seen.train_records + tr.seen.val_records}
        blob = container.storage.get(mv.artifact_uri)
        artifacts.verify(blob, uri=mv.artifact_uri, expected_sha256=mv.artifact_sha256, signature=mv.signature, key=KEY.encode())
        assert ModelRegistry(db).production("extractor") is None
        with pytest.raises(Conflict):
            ModelRegistry(db).promote(mv.id, promoted_by="x")


def test_training_without_signing_key_or_with_unsafe_format_is_refused(container):
    dsid = _dataset_with(container, _recs(8, 3))
    with container.session_factory() as db:
        with pytest.raises(artifacts.ArtifactError):
            run_training(db, container.storage, container.settings, StubTrainer(), db.get(TrainingDataset, dsid), policy=SMALL_OK)

        class Pickler(StubTrainer):
            def train(self, req):
                return TrainingResult(b"x", ".pkl", "evil", "2")
        with pytest.raises(artifacts.ArtifactError):
            run_training(db, container.storage, _settings(container), Pickler(), db.get(TrainingDataset, dsid), policy=SMALL_OK)


def test_ml_paths_never_log_document_text_or_secrets(client, container, tenant_a, caplog, run_queue):
    import logging
    caplog.set_level(logging.DEBUG)
    _publish(container, version="L", tamper=b'{"version":"evil"}')                 # forces the failure log path
    pid = submit_and_process(client, tenant_a, run_queue)
    client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": [{"field": "vendor", "value": "Secret Vendor Pvt"}]})
    with container.session_factory() as db:
        from app.ml.examples import ExampleService
        ExampleService(db, container.settings).set_policy(tenant_a.client_id, "tenant_only", by="t")
        ExampleService(db, container.settings).propose(tenant_a.client_id, pid)
        db.commit()
    _runtime(container).current()
    blob = "\n".join(r.getMessage() + " " + str(getattr(r, "__dict__", "")) for r in caplog.records)
    for forbidden in ("Secret Vendor Pvt", "Rice 25kg", "Cooking Oil", KEY, tenant_a.api_key, "4746"):
        assert forbidden not in blob, forbidden


class CrashingExtractor(RuleBasedExtractor):
    name, version = "crashy", "1"

    def extract(self, ocr, pages=None):
        raise RuntimeError("model exploded at /srv/models/secret.bin")


class CrashingModel(LayoutRulesExtractor):
    name, version = "crashy-model", "1"

    def extract(self, ocr, pages=None):
        raise RuntimeError("boom")


def test_extractor_crash_uses_reference_and_records_fallback(client, container, tenant_a, ocr, queue):
    from app.models import OcrResult
    ctx = WorkerContext(container.settings, container.session_factory, container.storage, ocr, CrashingModel())
    pid = upload(client, tenant_a).json()["process_id"]
    assert run_job(queue.items.pop(), ctx).outcome.value == "completed"
    body = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()
    assert body["model_versions"]["extractor"] == "rule-based-baseline@0.1.0"           # what really ran, not the crashed one
    with container.session_factory() as db:
        assert db.query(OcrResult).filter_by(process_id=pid).one().meta["fallback_from"] == "crashy-model@1"


def test_no_usable_extractor_fails_permanently_with_a_safe_message(client, container, tenant_a, ocr, queue):
    ctx = WorkerContext(container.settings, container.session_factory, container.storage, ocr, CrashingExtractor())
    pid = upload(client, tenant_a).json()["process_id"]
    assert run_job(queue.items.pop(), ctx).outcome.value == "failed"
    r = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h)
    e = r.json()["error"]
    assert e["code"] == "extraction_failed" and e["retryable"] is False and "secret" not in r.text and "/srv" not in r.text
    assert e["message"] == "The document could not be interpreted"
