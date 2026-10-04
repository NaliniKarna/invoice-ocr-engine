"""Training orchestration. Wraps a Trainer with the checks that make a result trustworthy and reproducible.

It never promotes anything: the output is a CANDIDATE model. Promotion needs an immutable evaluation on held-out data,
the configured gates, and an explicit approval (see registry.py).
"""
from __future__ import annotations

import hashlib
import platform
import random
import sys
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import Conflict
from app.ml import artifacts
from app.ml.dataset import load_dataset
from app.ml.interfaces import Trainer, TrainingRequest
from app.ml.registry import ModelRegistry
from app.ocr.preprocess_ops import parse_ops
from app.models import TrainingDataset
from app.storage.base import ObjectStorage, validate_key


class InsufficientTrainingData(Conflict):
    def __init__(self, detail: dict):
        super().__init__("Not enough approved data to train", code="insufficient_training_data")
        self.detail = detail


@dataclass(frozen=True)
class ReadinessPolicy:
    min_train_examples: int = 1000
    min_train_groups: int = 100
    min_val_examples: int = 100
    min_support_per_field: int = 50       # labelled occurrences of each critical field in train


def assess(records: list[dict], policy: ReadinessPolicy = ReadinessPolicy()) -> dict:
    train = [r for r in records if r["split"] == "train"]
    val = [r for r in records if r["split"] == "val"]
    support = {f: sum(1 for r in train if r["truth"].get(f) not in (None, "")) for f in ("vendor", "invoice_number", "date", "total")}
    problems = []
    if len(train) < policy.min_train_examples:
        problems.append({"code": "train_examples", "have": len(train), "need": policy.min_train_examples})
    groups = len({r["group"] for r in train})
    if groups < policy.min_train_groups:
        problems.append({"code": "train_groups", "have": groups, "need": policy.min_train_groups})
    if len(val) < policy.min_val_examples:
        problems.append({"code": "val_examples", "have": len(val), "need": policy.min_val_examples})
    for f, n in support.items():
        if n < policy.min_support_per_field:
            problems.append({"code": "field_support", "field": f, "have": n, "need": policy.min_support_per_field})
    return {"ready": not problems, "problems": problems, "train": len(train), "val": len(val), "groups": groups}


def run_training(db: Session, storage: ObjectStorage, settings: Settings, trainer: Trainer, dataset: TrainingDataset, *,
                 seed: int = 0, hyperparameters: dict | None = None, policy: ReadinessPolicy = ReadinessPolicy(),
                 parent_id: str | None = None):
    if dataset.kind != "training":
        raise Conflict("Only training datasets can be used to train", code="wrong_dataset_kind")
    if dataset.status != "active":
        raise Conflict("Dataset is flagged (consent withdrawn or erasure); rebuild it first", code="dataset_flagged")
    records = load_dataset(storage, dataset)            # verifies the checksum
    if any(r["split"] == "test" for r in records):
        raise Conflict("Training datasets must not contain the test split", code="split_leakage")
    ready = assess(records, policy)
    if not ready["ready"]:
        raise InsufficientTrainingData(ready)
    random.seed(seed)
    req = TrainingRequest([r for r in records if r["split"] == "train"], [r for r in records if r["split"] == "val"],
                          seed, hyperparameters or {})
    res = trainer.train(req)
    if not res.extension.lower() in artifacts.ALLOWED_EXTENSIONS:
        raise artifacts.ArtifactError("format_not_allowed")
    key = settings.ml_model_signing_key.get_secret_value().encode()
    if not key:
        raise artifacts.ArtifactError("no_signing_key")
    digest = hashlib.sha256(res.artifact).hexdigest()
    uri = validate_key(f"models/{res.model_name}/{digest}{res.extension.lower()}")
    storage.put(uri, res.artifact, "application/octet-stream")
    from app.core.version import APP_VERSION, code_fingerprint
    from app.extraction.normalization import NORMALIZATION_VERSION
    from app.ml.evaluation import environment_info
    from app.ocr.preprocess import PREPROCESS_VERSION
    from app.ocr.preprocess_ops import preprocess_version
    meta = {"app_version": APP_VERSION, "code_sha256": code_fingerprint(), "libraries": environment_info()["libraries"],
            "schema_version": res.schema_version, "normalization_version": NORMALIZATION_VERSION,
            "preprocessing_version": preprocess_version(PREPROCESS_VERSION, parse_ops(settings.preprocess_ops)),
            "base_model_id": parent_id, "dataset_split_spec": dataset.split_spec, "dataset_scope": dataset.scope,
            "seed": seed, "hyperparameters": hyperparameters or {}, "trainer": f"{trainer.name}@{trainer.version}",
            "dataset_id": dataset.id, "dataset_sha256": dataset.checksum_sha256, "python": sys.version.split()[0],
            "platform": platform.platform(), "train_examples": ready["train"], "val_examples": ready["val"],
            "training_metrics_note": "training/validation numbers only; not promotion evidence", **res.training_metrics}
    return ModelRegistry(db).register_candidate(
        kind="extractor", name=res.model_name, version=digest[:12], artifact_uri=uri, dataset_id=dataset.id, parent_id=parent_id,
        schema_version=res.schema_version, artifact_sha256=digest, signature=artifacts.sign(key, digest), training_meta=meta)
