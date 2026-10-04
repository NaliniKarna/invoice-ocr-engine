"""Metrics, splits, calibration, artifacts, external-provider policy, comparison statistics. Pure unit tests."""
import hashlib
import random

import pytest

from app.core.config import Settings
from app.ml import artifacts, metrics as M
from app.ml.calibration import InsufficientCalibrationData, IsotonicCalibrator, fit_and_validate, fit_isotonic
from app.ml.evaluation import MIN_DOCS_FOR_COMPARISON, compare
from app.ml.external import decide
from app.ml.quality import QualityConfig, check_example, content_hash, group_key_for
from app.ml.registry import PromotionGates
from app.ml.splits import SplitSpec, assign, leakage_report, split_examples, split_summary


# ------------------------------------------------------------------ metrics
def _dp(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def test_edit_distance_matches_reference_dp():
    rng = random.Random(3)
    for _ in range(200):
        a = "".join(rng.choice("abc ") for _ in range(rng.randrange(0, 40)))
        b = "".join(rng.choice("abc ") for _ in range(rng.randrange(0, 40)))
        assert M.edit_distance(a, b) == _dp(a, b)
    long_a, long_b = "x" * 300 + "abc", "x" * 298 + "yyabc"      # longer than one machine word
    assert M.edit_distance(long_a, long_b) == _dp(long_a, long_b)


def test_cer_wer_definitions():
    assert M.cer("abcd", "abcd") == 0 and M.cer("abcd", "abxd") == 0.25
    assert M.wer("the quick fox", "the slow fox") == pytest.approx(1 / 3)
    assert M.cer("", "") == 0


def test_numeric_and_date_token_accuracy():
    assert M.numeric_char_accuracy("Total 1,234.50", "Total 1,234.50") == 1.0
    assert M.numeric_char_accuracy("Total 1,234.50", "Total 1,284.50") < 1.0
    assert M.date_token_accuracy("Date: 15/03/2026", "Date: 15/03/2026") == 1.0
    assert M.date_token_accuracy("Date: 15/03/2026", "Date: 15/08/2026") == 0.0
    assert M.date_token_accuracy("no date here", "no date here") is None


def test_values_equal_is_type_aware():
    assert M.values_equal("total", "100.00", "100") and not M.values_equal("total", "100.00", "100.01")
    assert M.values_equal("vendor", "Alpha  Traders", "alpha traders")
    assert M.values_equal("date", "2026-03-15", "2026-03-15") and not M.values_equal("date", "2026-03-15", "2026-03-16")
    assert not M.values_equal("total", "100", None)


def test_field_metrics_precision_recall_and_false_positives():
    pairs = [({"vendor": "A", "total": "10"}, {"vendor": "A", "total": "11"}),       # total wrong -> fp + fn
             ({"vendor": "B"}, {"vendor": "B", "currency": "NPR"})]                  # currency hallucinated -> fp
    r = M.field_metrics(pairs)
    assert r["per_field"]["vendor"]["f1"] == 1.0
    assert r["per_field"]["total"]["tp"] == 0 and r["per_field"]["total"]["fn"] == 1 and r["per_field"]["total"]["fp"] == 1
    assert r["per_field"]["currency"]["fp"] == 1
    assert r["micro"]["precision"] == pytest.approx(2 / 4) and r["micro"]["recall"] == pytest.approx(2 / 3)


def test_financial_metrics_tolerance_and_error():
    from decimal import Decimal
    pairs = [({"total": "100.00"}, {"total": "100.004"}), ({"total": "200.00"}, {"total": "210.00"}), ({"total": "5"}, {})]
    t = M.financial_metrics(pairs, Decimal("0.01"))["total"]
    assert t["support"] == 3 and t["tolerance_accuracy"] == pytest.approx(1 / 3) and t["missing"] == 1
    assert t["exact_accuracy"] == 0


def test_line_items_measured_separately_from_header():
    truth = {"items": [{"description": "Rice", "quantity": "2", "unit_price": "10", "amount": "20"},
                       {"description": "Oil", "quantity": "1", "unit_price": "5", "amount": "5"}]}
    perfect = M.line_item_metrics([(truth, truth)])
    assert perfect["row_detection"]["f1"] == 1.0 and perfect["aggregate_row_accuracy"] == 1.0
    miss = {"items": [truth["items"][0]]}
    r = M.line_item_metrics([(truth, miss)])
    assert r["row_detection"]["recall"] == 0.5 and r["item_count_exact"] == 0.0
    wrong_amount = {"items": [dict(truth["items"][0], amount="21"), truth["items"][1]]}
    r = M.line_item_metrics([(truth, wrong_amount)])
    assert r["row_detection"]["f1"] == 1.0 and r["aggregate_row_accuracy"] == 0.5 and r["field_accuracy"]["amount"] == 0.5


def test_reliability_ece_perfect_and_overconfident():
    perfect = M.reliability([1.0] * 10 + [0.0] * 10, [True] * 10 + [False] * 10)
    assert perfect["ece"] == pytest.approx(0, abs=1e-9)
    over = M.reliability([0.95] * 100, [True] * 50 + [False] * 50)
    assert over["ece"] == pytest.approx(0.45, abs=1e-6)


def test_percentile_uses_percent_scale():
    assert M.percentile([1, 2, 3, 4, 5], 50) == 3 and M.percentile([1, 2, 3, 4, 5], 100) == 5 and M.percentile([], 50) is None


# ------------------------------------------------------------------ splits
def test_split_is_deterministic_and_seed_dependent():
    items = [(f"e{i}", f"g{i % 40}") for i in range(300)]
    a, b = split_examples(items, SplitSpec(seed="x")), split_examples(items, SplitSpec(seed="x"))
    assert a == b and a != split_examples(items, SplitSpec(seed="y"))


def test_split_never_separates_a_group_and_is_stable_when_data_grows():
    items = [(f"e{i}", f"g{i % 40}") for i in range(300)]
    groups = dict((e, g) for e, g in items)
    a = split_examples(items, SplitSpec(seed="x"))
    assert leakage_report(a, groups)["ok"]
    more = items + [(f"n{i}", f"g{i % 40}") for i in range(100)]           # new docs of existing groups
    b = split_examples(more, SplitSpec(seed="x"))
    assert all(b[e] == a[e] for e, _ in items) and all(b[f"n{i}"] == a[f"e{i % 40}"] for i in range(40))


def test_split_ratios_roughly_respected_on_large_data_and_bad_ratios_rejected():
    items = [(f"e{i}", f"g{i}") for i in range(5000)]
    a = split_examples(items, SplitSpec(seed="q", train=0.7, val=0.15, test=0.15))
    frac = sum(v == "test" for v in a.values()) / len(a)
    assert 0.12 < frac < 0.18
    with pytest.raises(ValueError):
        split_examples(items, SplitSpec(train=0.5, val=0.5, test=0.5))


def test_small_eval_split_is_reported_as_not_meaningful():
    items = [(f"e{i}", f"g{i}") for i in range(20)]
    a = split_examples(items, SplitSpec(seed="s"))
    rep = split_summary(a, dict(items), SplitSpec(seed="s"))
    assert rep["warnings"] and "not statistically meaningful" in rep["warnings"][0]


def test_leakage_report_detects_a_leak():
    assert not leakage_report({"a": "train", "b": "test"}, {"a": "g", "b": "g"})["ok"]


def test_group_key_is_tenant_scoped_and_name_normalised():
    a = group_key_for({"vendor": "Alpha  Traders"}, tenant_id="t1")
    assert a == group_key_for({"vendor": "alpha traders"}, tenant_id="t1") and a != group_key_for({"vendor": "alpha traders"}, tenant_id="t2")


# ------------------------------------------------------------------ quality gates
GOOD = {"vendor": "A", "date": "2026-03-15", "currency": "NPR", "subtotal": "100", "tax": "13", "total": "113", "items": []}


def test_quality_gate_accepts_clean_example_and_flags_each_defect_without_fixing():
    assert [i for i in check_example(GOOD, schema_version="2") if i["severity"] == "error"] == []

    def codes(truth, **kw):
        return {i["code"] for i in check_example(truth, schema_version=kw.pop("schema_version", "2"), **kw)}
    assert "missing_required_field" in codes({**GOOD, "vendor": None})
    assert "malformed_annotation" in codes({**GOOD, "total": "abc"})
    assert "validation:totals_mismatch" in codes({**GOOD, "total": "999"})
    assert "validation:date_out_of_range" in codes({**GOOD, "date": "1990-01-01"})
    assert "schema_mismatch" in codes(GOOD, schema_version="9")
    assert "unusable_ocr" in codes(GOOD, ocr_chars=3)
    assert "pages_failed" in codes(GOOD, pages_total=3, pages_failed=1)
    assert "contradictory_annotation" in codes(GOOD, conflicting_fields=["total"])
    assert "duplicate_example" in codes(GOOD, duplicate_of="ex_1")
    assert GOOD["total"] == "113"                                                # input untouched


def test_content_hash_is_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


# ------------------------------------------------------------------ calibration
def test_calibration_refuses_small_data():
    with pytest.raises(InsufficientCalibrationData):
        fit_and_validate([0.9] * 100, [True] * 100)


def test_calibration_reduces_ece_on_holdout_for_overconfident_scores():
    # Mathematical check on SIMULATED scores (true accuracy = conf^2). It validates the method, not the product's confidence.
    rng = random.Random(5)
    conf = [rng.random() for _ in range(3000)]
    correct = [rng.random() < c * c for c in conf]
    cal, rep = fit_and_validate(conf, correct, seed=1)
    assert rep["ece_after"] < rep["ece_before"] / 2
    ys = cal.ys
    assert ys == sorted(ys)                                                       # monotone
    assert IsotonicCalibrator.from_dict(cal.to_dict()).apply(0.9) == cal.apply(0.9)


def test_calibrator_rejects_tampered_parameters():
    with pytest.raises(ValueError):
        IsotonicCalibrator.from_dict({"method": "isotonic", "xs": [0.5, 0.1], "ys": [0.1, 0.2]})
    with pytest.raises(ValueError):
        IsotonicCalibrator.from_dict({"method": "pickle", "xs": [], "ys": []})


# ------------------------------------------------------------------ artifacts
KEY = b"k" * 32


def _signed(blob):
    d = hashlib.sha256(blob).hexdigest()
    return d, artifacts.sign(KEY, d)


def test_artifact_happy_path_and_tampering():
    blob = b'{"w":[1,2,3]}'
    d, sig = _signed(blob)
    artifacts.verify(blob, uri="models/x.json", expected_sha256=d, signature=sig, key=KEY)
    for bad, code in ((b'{"w":[9]}', "checksum_mismatch"),):
        with pytest.raises(artifacts.ArtifactError) as e:
            artifacts.verify(bad, uri="models/x.json", expected_sha256=d, signature=sig, key=KEY)
        assert e.value.reason == code
    with pytest.raises(artifacts.ArtifactError) as e:
        artifacts.verify(blob, uri="models/x.json", expected_sha256=d, signature="0" * 64, key=KEY)
    assert e.value.reason == "bad_signature"
    with pytest.raises(artifacts.ArtifactError) as e:
        artifacts.verify(blob, uri="models/x.json", expected_sha256=d, signature=sig, key=b"")
    assert e.value.reason == "no_signing_key"
    with pytest.raises(artifacts.ArtifactError) as e:                               # signed with a different key
        artifacts.verify(blob, uri="models/x.json", expected_sha256=d, signature=artifacts.sign(b"z" * 32, d), key=KEY)
    assert e.value.reason == "bad_signature"


@pytest.mark.parametrize("uri", ["m/model.pkl", "m/model.pickle", "m/model.joblib", "m/model.pt", "m/model.bin", "m/model", None])
def test_unsafe_or_unknown_formats_refused_by_extension(uri):
    with pytest.raises(artifacts.ArtifactError) as e:
        artifacts.check_uri(uri)
    assert e.value.reason == "format_not_allowed"


def test_pickle_payload_refused_even_with_valid_signature_and_safe_extension():
    import pickle
    blob = pickle.dumps({"a": 1})
    d, sig = _signed(blob)
    with pytest.raises(artifacts.ArtifactError) as e:
        artifacts.verify(blob, uri="models/disguised.json", expected_sha256=d, signature=sig, key=KEY)
    assert e.value.reason == "unsafe_format"


def test_parse_json_rejects_non_objects_and_garbage():
    assert artifacts.parse_json(b'{"a":1}') == {"a": 1}
    for bad in (b"[1]", b"not json", b"\x80\x04."):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.parse_json(bad)


# ------------------------------------------------------------------ external providers
def _settings(**kw):
    return Settings(api_key_pepper="p" * 32, admin_token="a" * 32, database_url="sqlite://", **kw)


def test_external_providers_denied_by_default_and_need_three_conditions():
    assert decide(_settings(), provider="acme-llm", tenant_consented=True).reason == "external_providers_disabled"
    on = dict(ml_external_providers_enabled=True, ml_external_provider_allowlist="acme-llm")
    assert decide(_settings(**on), provider="other", tenant_consented=True).reason == "provider_not_allow_listed"
    assert decide(_settings(**on), provider="acme-llm", tenant_consented=False).reason == "tenant_not_consented"
    assert decide(_settings(**on), provider="acme-llm", tenant_consented=True).allowed


# ------------------------------------------------------------------ comparison statistics
def test_compare_refuses_small_samples():
    r = compare({f"d{i}": 0.5 for i in range(10)}, {f"d{i}": 0.9 for i in range(10)})
    assert r["verdict"] == "insufficient_data" and "noise" in r["note"]


def test_compare_detects_clear_difference_and_calls_noise_inconclusive():
    n = MIN_DOCS_FOR_COMPARISON * 3
    rng = random.Random(1)
    a = {f"d{i}": rng.random() for i in range(n)}
    assert compare(a, {k: min(1.0, v + 0.3) for k, v in a.items()})["verdict"] == "b_better"
    assert compare({k: min(1.0, v + 0.3) for k, v in a.items()}, a)["verdict"] == "a_better"
    noisy = {k: v + rng.choice((-0.2, 0.2)) for k, v in a.items()}
    assert compare(a, noisy)["verdict"] == "inconclusive"
    assert compare(a, noisy, seed=1) == compare(a, noisy, seed=1)                # deterministic


def test_default_gates_are_strict():
    g = PromotionGates()
    assert not g.allow_synthetic_evidence and g.min_samples >= 200 and g.max_regression == 0.0
