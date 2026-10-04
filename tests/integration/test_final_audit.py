"""Release-audit (Prompt 4) hostile tests. They add coverage for gaps found while auditing; nothing here replaces an
existing test.

Covered here: every operator ML endpoint against every credential failure mode, throttling of operator-token guessing,
foreign-vs-unknown indistinguishability on every mutating verb, tenant isolation of the public surface, key and token
secrecy in logs, model-artifact location/traversal rules, and ML governance invariants (immutability of machine results,
withdrawal impact is identifiable).
"""
import hashlib
import json
import logging

import pytest
from pydantic import SecretStr

from app.api.internal import ml as ml_api
from app.ml import artifacts
from app.extraction.rules import RuleBasedExtractor
from app.ml.registry import ModelRegistry
from app.models import ModelVersion, OcrResult
from tests.conftest import ADMIN, make_tenant
from tests.helpers import submit_and_process, upload
from tests.integration.test_ml_runtime import KEY, _publish, _runtime, _settings  # noqa: F401  (shared model fixtures)

ML_ROUTES = sorted({(m, r.path) for r in ml_api.router.routes for m in r.methods})


def _concrete(path: str) -> str:
    return path.replace("{client_id}", "cli_x").replace("{example_id}", "ex_x").replace("{dataset_id}", "ds_x") \
               .replace("{model_id}", "mv_x")


def test_operator_ml_surface_is_eleven_paths_and_twelve_operations():
    # GET and POST /examples share a path. A new ML route must be added to this audit on purpose.
    assert len({p for _, p in ML_ROUTES}) == 11
    assert len(ML_ROUTES) == 12


def _creds(container, tenant):
    """Every credential shape an attacker or a misconfigured client could present."""
    good = container.settings.admin_token.get_secret_value()
    return {
        "none": {},
        "empty_admin_token": {"X-Admin-Token": ""},
        "wrong_admin_token": {"X-Admin-Token": "wrong"},
        "case_changed_token": {"X-Admin-Token": good.upper()},
        "token_with_suffix": {"X-Admin-Token": good + " "},
        "token_prefix_only": {"X-Admin-Token": good[:-1]},
        "tenant_key_as_api_key": tenant.h,
        "tenant_key_as_admin_token": {"X-Admin-Token": tenant.api_key},
        "tenant_key_as_bearer": {"Authorization": f"Bearer {tenant.api_key}"},
        "admin_token_as_api_key": {"X-API-Key": good},
    }


@pytest.mark.parametrize("method,path", ML_ROUTES)
def test_operator_ml_endpoint_rejects_every_non_operator_credential(client, container, tenant_a, method, path):
    # Raise the budget so the failed-auth throttle (tested separately) does not turn 401 into 429 in this matrix.
    container.settings.auth_failures_per_minute = 10_000
    for label, headers in _creds(container, tenant_a).items():
        r = client.request(method, _concrete(path), headers=headers, json={})
        assert r.status_code == 401, (label, r.status_code)
        body = r.json()["error"]
        assert body["code"] == "invalid_admin_token", label
        assert tenant_a.api_key not in r.text and container.settings.admin_token.get_secret_value() not in r.text


@pytest.mark.parametrize("method,path", ML_ROUTES)
def test_operator_ml_endpoint_rejects_revoked_and_underscoped_tenant_keys(client, container, tenant_a, method, path):
    container.settings.auth_failures_per_minute = 10_000
    ro = make_tenant(container, "readonly", scopes=["bills:read"])
    admin_only_scopes = make_tenant(container, "scoped", scopes=["bills:read", "bills:write", "feedback:write"])
    revoked = make_tenant(container, "revoked")
    assert client.post(f"/internal/v1/clients/{revoked.client_id}/api-keys/{revoked.key_id}/revoke", headers=ADMIN).status_code in (200, 204)
    for t in (ro, admin_only_scopes, revoked):
        for hdr in (t.h, {"X-Admin-Token": t.api_key}):
            assert client.request(method, _concrete(path), headers=hdr, json={}).status_code == 401


@pytest.mark.parametrize("method,path", ML_ROUTES)
def test_operator_token_is_accepted_everywhere_it_should_be(client, container, method, path):
    """Positive control: the matrix above is meaningful only if the correct token passes authentication."""
    r = client.request(method, _concrete(path), headers=ADMIN, json={})
    assert r.status_code != 401


def test_operator_token_guessing_is_throttled(client, container):
    codes = [client.get("/internal/v1/models", headers={"X-Admin-Token": f"guess-{i}"}).status_code for i in range(9)]
    assert codes[:5] == [401] * 5 and 429 in codes[5:], codes
    assert client.get("/internal/v1/models", headers={"X-Admin-Token": "x"}).headers.get("Retry-After")


def operations(client):
    """(METHOD, path) for every documented operation. Derived from the OpenAPI document because FastAPI 0.142 keeps included
    routers lazy, so iterating `app.routes` yields no concrete paths (an earlier structural test silently checked nothing)."""
    spec = client.get("/openapi.json").json()
    return sorted((m.upper(), p) for p, ops in spec["paths"].items() for m in ops if m in ("get", "post", "put", "delete", "patch"))


def test_every_documented_operation_rejects_an_unauthenticated_call(client, container):
    container.settings.auth_failures_per_minute = 10_000
    ops = operations(client)
    internal = [o for o in ops if o[1].startswith("/internal/")]
    public = [o for o in ops if o[1].startswith("/api/")]
    assert len(internal) == 6 + 12 and len(public) == 7, (len(internal), len(public))   # 6 admin + 12 ML ops; fails if a route is added unaudited
    for m, p in internal + public:
        path = _concrete(p).replace("{process_id}", "0" * 20).replace("{batch_id}", "batch_" + "0" * 32) \
            .replace("{key_id}", "key_x")
        r = client.request(m, path, json={})
        assert r.status_code == 401, (m, p, r.status_code)


# ---------------------------------------------------------------- tenant isolation of the public surface
def _normalise(r):
    body = r.json()
    body.get("error", {}).pop("request_id", None)
    return r.status_code, body


def test_foreign_and_unknown_resources_are_identical_on_every_verb(client, tenant_a, tenant_b, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    unknown = "0" * 20
    for build in (
        lambda p: client.get(f"/api/v1/bills/{p}", headers=tenant_b.h),
        lambda p: client.delete(f"/api/v1/bills/{p}", headers=tenant_b.h),
        lambda p: client.post(f"/api/v1/bills/{p}/cancel", headers=tenant_b.h),
        lambda p: client.post(f"/api/v1/bills/{p}/feedback", headers=tenant_b.h,
                              json={"corrections": [{"field": "vendor", "value": "X"}]}),
    ):
        assert _normalise(build(pid)) == _normalise(build(unknown))
    # And A still owns an intact result after B's attempts (no side effects).
    assert client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).json()["status"] == "completed"


def test_foreign_batch_equals_unknown_batch(client, tenant_a, tenant_b, queue):
    r = client.post("/api/v1/bills/bulk", headers=tenant_a.h, files=[("files", ("a.png", __import__("tests.fakes", fromlist=["png_bytes"]).png_bytes(), "image/png"))])
    assert r.status_code == 202
    bid = r.json()["batch_id"]
    foreign = client.get(f"/api/v1/bills/batches/{bid}", headers=tenant_b.h)
    unknown = client.get(f"/api/v1/bills/batches/batch_{'0' * 32}", headers=tenant_b.h)
    assert _normalise(foreign) == _normalise(unknown) and foreign.status_code == 404


def test_public_result_never_exposes_storage_tenant_or_model_artifact_details(client, container, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    text = client.get(f"/api/v1/bills/{pid}", headers=tenant_a.h).text
    for forbidden in ("tenants/", tenant_a.client_id, "artifact", "signature", "models/", "storage", "training", "example"):
        assert forbidden.lower() not in text.lower(), forbidden


def test_tenant_key_reaches_no_operator_operation(client, container, tenant_a):
    container.settings.auth_failures_per_minute = 10_000
    internal = [o for o in operations(client) if o[1].startswith("/internal/")]
    assert internal
    for m, p in internal:
        r = client.request(m, _concrete(p).replace("{key_id}", "key_x"), headers=tenant_a.h, json={})
        assert r.status_code == 401, (m, p)


def test_one_tenants_feedback_never_appears_in_another_tenants_results(client, container, tenant_a, tenant_b, run_queue):
    pa = submit_and_process(client, tenant_a, run_queue)
    pb = submit_and_process(client, tenant_b, run_queue)
    assert client.post(f"/api/v1/bills/{pa}/feedback", headers=tenant_a.h,
                       json={"corrections": [{"field": "vendor", "value": "A SECRET VENDOR"}]}).status_code == 201
    assert "A SECRET VENDOR" not in client.get(f"/api/v1/bills/{pb}", headers=tenant_b.h).text


# ---------------------------------------------------------------- secrets in logs
def test_api_keys_and_admin_token_never_reach_logs_on_success_or_failure(client, container, tenant_a, caplog, run_queue):
    caplog.set_level(logging.DEBUG)
    admin_token = container.settings.admin_token.get_secret_value()
    secret_part = tenant_a.api_key.rsplit("_", 1)[-1]
    client.get("/api/v1/bills/" + "0" * 20, headers={"X-API-Key": tenant_a.api_key[:-3] + "xyz"})      # near miss
    client.get("/api/v1/bills/" + "0" * 20, headers=tenant_a.h)
    client.get("/internal/v1/models", headers={"X-Admin-Token": admin_token})
    client.get("/internal/v1/models", headers={"X-Admin-Token": admin_token + "x"})
    submit_and_process(client, tenant_a, run_queue)
    logs = caplog.text
    assert tenant_a.api_key not in logs and secret_part not in logs and admin_token not in logs


def test_near_miss_key_with_valid_prefix_is_rejected_like_any_other(client, tenant_a):
    bad = tenant_a.api_key[:-1] + ("0" if tenant_a.api_key[-1] != "0" else "1")
    r = client.get("/api/v1/bills/" + "0" * 20, headers={"X-API-Key": bad})
    s = client.get("/api/v1/bills/" + "0" * 20, headers={"X-API-Key": "ocr_nonexistent_" + "a" * 64})
    assert r.status_code == s.status_code == 401
    assert r.json()["error"]["message"] == s.json()["error"]["message"]


# ---------------------------------------------------------------- model artifact location and traversal
class _CountingStorage:
    def __init__(self, inner):
        self.inner, self.gets = inner, []

    def get(self, key):
        self.gets.append(key)
        return self.inner.get(key)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.mark.parametrize("uri", [
    "models/../tenants/cli_x/documents/proc_y/original.json",
    "../models/x/abc.json", "/etc/passwd.json", "models\\x\\abc.json",
    "tenants/cli_x/documents/proc_y/original.json",              # another tenant's object, right extension
    "datasets/pooled/abc.json", "models//abc.json", "models/./abc.json",
])
def test_artifact_must_live_under_models_and_is_never_read_otherwise(container, uri):
    from app.extraction.rules import RuleBasedExtractor
    from app.ml.runtime import ModelRuntime
    blob = json.dumps({"version": "1"}).encode()
    digest = hashlib.sha256(blob).hexdigest()
    with container.session_factory() as db:
        reg = ModelRegistry(db)
        mv = reg.register_candidate(kind="extractor", name="json-model", version="1", artifact_uri=uri, dataset_id=None,
                                    schema_version="2", artifact_sha256=digest, signature=artifacts.sign(KEY.encode(), digest))
        mv.status = "approved"; reg.promote(mv.id, promoted_by="test")
        db.commit()
    storage = _CountingStorage(container.storage)
    rt = ModelRuntime(_settings(container), container.session_factory, storage, RuleBasedExtractor(), refresh_seconds=0)
    assert rt.current().name == "rule-based-baseline"
    assert rt.status()["last_error"] in ("bad_location", "format_not_allowed"), rt.status()
    assert storage.gets == []                    # rejected before any read


def test_nul_byte_location_is_rejected_by_the_check(container):
    with pytest.raises(artifacts.ArtifactError):
        artifacts.check_uri("models/x/\x00abc.json")


def test_signed_but_pickle_content_is_refused_even_with_an_allowed_extension(container):
    evil = b"\x80\x04\x95" + b"cos\nsystem\n" * 3                         # pickle protocol 4 header
    _publish(container, blob=evil)
    rt = _runtime(container)
    assert rt.current().name == "rule-based-baseline" and rt.status()["last_error"] == "unsafe_format"


def test_oversized_artifact_is_refused(container, monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_ARTIFACT_BYTES", 10)
    _publish(container, blob=json.dumps({"version": "1", "pad": "x" * 100}).encode())
    rt = _runtime(container)
    assert rt.status() is not None and rt.current().name == "rule-based-baseline" and rt.status()["last_error"] == "too_large"


def test_a_signed_candidate_that_was_never_promoted_is_never_served(container):
    _publish(container, production=False)
    assert _runtime(container).current().name == "rule-based-baseline"


def test_signature_from_another_deployment_key_is_refused(container):
    _publish(container, sign_with="another-deployments-key-" + "q" * 8)
    assert _runtime(container).current().name == "rule-based-baseline"


# ---------------------------------------------------------------- governance invariants
def test_machine_result_is_untouched_by_feedback_and_dataset_activity(client, container, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    with container.session_factory() as db:
        before = [(r.data, r.confidence, r.model_version_id if hasattr(r, "model_version_id") else None)
                  for r in db.query(OcrResult).all()]
    assert client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h,
                       json={"corrections": [{"field": "vendor", "value": "Changed Vendor"}]}).status_code == 201
    client.put(f"/internal/v1/clients/{tenant_a.client_id}/training-policy", headers=ADMIN, json={"policy": "pooled"})
    client.post("/internal/v1/examples", headers=ADMIN, json={"client_id": tenant_a.client_id, "process_id": pid})
    with container.session_factory() as db:
        after = [(r.data, r.confidence, r.model_version_id if hasattr(r, "model_version_id") else None)
                 for r in db.query(OcrResult).all()]
    assert before == after


def test_feedback_alone_cannot_change_which_model_serves(client, container, tenant_a, run_queue):
    pid = submit_and_process(client, tenant_a, run_queue)
    with container.session_factory() as db:
        before = db.query(ModelVersion).filter_by(status="production").count()
    client.post(f"/api/v1/bills/{pid}/feedback", headers=tenant_a.h, json={"corrections": [{"field": "vendor", "value": "Z"}]})
    with container.session_factory() as db:
        assert db.query(ModelVersion).filter_by(status="production").count() == before


def test_secrets_setting_is_not_serialised(container):
    s = container.settings
    assert isinstance(s.admin_token, SecretStr) and isinstance(s.api_key_pepper, SecretStr)
    assert s.admin_token.get_secret_value() not in repr(s) and s.admin_token.get_secret_value() not in s.model_dump_json()


# ---------------------------------------------------------------- unsupported script (Devanagari)
def _pipeline(lines):
    from app.extraction.rules import RuleBasedExtractor
    from app.extraction.pipeline import DocumentPipeline
    from PIL import Image
    from tests.fakes import FakeOCR
    return DocumentPipeline(FakeOCR(lines=lines), RuleBasedExtractor(), day_first=True, review_threshold=0.8), [Image.new("RGB", (50, 50), "white")]


NEPALI = ["श्री हिमालयन ट्रेडर्स प्रा. लि.", "बिल नं: १२३४", "मिति: २०८३/०६/१५", "जम्मा रकम: ४,७४६.००"]


def test_devanagari_text_is_flagged_unsupported_and_forces_review():
    pipe, imgs = _pipeline(NEPALI + ["Total 4,746.00"])
    out = pipe.run(imgs)
    assert "unsupported_script" in {i.code for i in out.extraction.issues}
    assert out.extraction.requires_review is True


def test_latin_documents_never_get_the_unsupported_script_issue():
    pipe, imgs = _pipeline(["Himalayan Traders Pvt Ltd", "Invoice No: INV-1", "Date: 15/03/2026", "Total 4,746.00"])
    assert "unsupported_script" not in {i.code for i in pipe.run(imgs).extraction.issues}


def test_a_few_devanagari_digits_in_an_otherwise_latin_invoice_are_not_flagged():
    pipe, imgs = _pipeline(["Himalayan Traders Pvt Ltd", "Invoice No: INV-१२", "Date: 15/03/2026", "Total 4,746.00"])
    assert "unsupported_script" not in {i.code for i in pipe.run(imgs).extraction.issues}


# ---------------------------------------------------------------- withdrawal impact is identifiable (not automated)
def test_models_trained_on_withdrawn_data_are_identified_for_manual_retraining(client, container, run_queue):
    from app.ml.dataset import build_dataset
    from tests.integration.test_ml_pipeline import make_example, policy
    t = make_tenant(container, "withdrawn-impact")
    policy(container, t, "tenant_only")
    make_example(client, container, run_queue, t, "Alpha Traders")
    with container.session_factory() as db:
        ds = build_dataset(db, container.storage, container.settings, scope=f"tenant:{t.client_id}")
        ds_id = ds.id
        reg = ModelRegistry(db)
        parent = reg.register_candidate(kind="extractor", name="m", version="1", artifact_uri=None, dataset_id=ds_id)
        child = reg.register_candidate(kind="extractor", name="m", version="2", artifact_uri=None, dataset_id=None, parent_id=parent.id)
        unrelated = reg.register_candidate(kind="extractor", name="other", version="1", artifact_uri=None, dataset_id=None)
        ids = (parent.id, child.id, unrelated.id)
        db.commit()
    before = {m["model_id"]: m for m in client.get("/internal/v1/models", headers=ADMIN).json()["items"]}
    assert not any(before[i]["affected_by_withdrawal"] for i in ids)
    policy(container, t, "none")                        # consent withdrawn
    after = {m["model_id"]: m for m in client.get("/internal/v1/models", headers=ADMIN).json()["items"]}
    assert after[ids[0]]["affected_by_withdrawal"] and after[ids[0]]["flagged_datasets"] == [ds_id]
    assert after[ids[1]]["affected_by_withdrawal"]       # fine-tuned from the affected parent
    assert not after[ids[2]]["affected_by_withdrawal"]
    assert "no automatic unlearning" in after[ids[0]]["action_required"]
    # and the flagged dataset can no longer be used for training
    with container.session_factory() as db:
        from app.models import TrainingDataset
        assert db.get(TrainingDataset, ds_id).status == "flagged_withdrawal"


# ---------------------------------------------------------------- schema invariants (run on PostgreSQL via Alembic when configured)
def test_every_tenant_owned_table_has_a_foreign_key_on_client_id(container):
    from sqlalchemy import inspect
    insp = inspect(container.engine)
    allowed_without_fk = {"audit_logs"}      # by design: audit records must outlive the tenant
    for table in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns(table)}
        if "client_id" in cols and table not in allowed_without_fk:
            fks = [fk for fk in insp.get_foreign_keys(table) if "client_id" in fk["constrained_columns"]]
            assert fks, f"{table} has client_id but no foreign key on it"


def test_derived_ml_tables_reference_their_owner_rows(container):
    from sqlalchemy import inspect
    insp = inspect(container.engine)
    refs = {t: {fk["referred_table"] for fk in insp.get_foreign_keys(t)} for t in ("dataset_examples", "training_examples", "evaluation_runs")}
    assert "training_examples" in refs["dataset_examples"] and "training_datasets" in refs["dataset_examples"]
    assert "processes" in refs["training_examples"]      # composite (client_id, process_id): the DB refuses cross-tenant examples
    assert "model_versions" in refs["evaluation_runs"]


# ---------------------------------------------------------------- real OCR engine in the suite (skipped when Tesseract is absent)
import shutil  # noqa: E402

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")


@needs_tesseract
def test_real_tesseract_through_the_production_pipeline_reads_a_typeset_invoice():
    from app.core.config import Settings
    from app.extraction.layout_rules import LayoutRulesExtractor
    from app.extraction.pipeline import DocumentPipeline
    from app.ocr.providers import build_ocr_provider
    from scripts.e2e_smoke import invoice_png
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(invoice_png())).convert("RGB")
    ocr = build_ocr_provider(Settings(env="test", database_url="sqlite://", redis_url="redis://x", ocr_provider="tesseract"))
    for ex in (RuleBasedExtractor(), LayoutRulesExtractor()):
        out = DocumentPipeline(ocr, ex, day_first=True, review_threshold=0.8).run([img])
        d = out.extraction.data
        assert (str(d.total), str(d.subtotal), str(d.tax), d.invoice_number) == ("4746.00", "4200.00", "546.00", "INV-2026-0042"), ex.name
        assert out.extraction.confidence_method == "heuristic-v2" and out.extraction.calibrated is False
        assert out.ocr.provider == "tesseract" and out.failed_pages == []


@needs_tesseract
def test_real_tesseract_blank_page_is_not_reported_as_a_confident_result():
    from app.core.config import Settings
    from app.extraction.pipeline import DocumentPipeline
    from app.ocr.providers import build_ocr_provider
    from PIL import Image
    ocr = build_ocr_provider(Settings(env="test", database_url="sqlite://", redis_url="redis://x", ocr_provider="tesseract"))
    out = DocumentPipeline(ocr, RuleBasedExtractor(), day_first=True, review_threshold=0.8).run([Image.new("RGB", (600, 400), "white")])
    assert out.extraction.requires_review is True and out.extraction.data.total is None
