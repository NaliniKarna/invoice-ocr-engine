"""Integration points for a trainable extractor. No trainer ships with this code base: there is no representative
labelled dataset, so training anything would produce a model nobody could trust (see docs/BENCHMARK.md).
`training.run_training` owns everything around a trainer (dataset checks, reproducibility metadata, signing,
registration); a Trainer implementation only has to produce artifact bytes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TrainingRequest:
    train_records: list[dict]          # split == "train"; ground-truth fields only, no OCR text
    val_records: list[dict]            # split == "val"; for early stopping / model selection. NEVER the test split.
    seed: int
    hyperparameters: dict = field(default_factory=dict)
    base_artifact: bytes | None = None


@dataclass
class TrainingResult:
    artifact: bytes                    # data format only (JSON / safetensors / ONNX); never a pickle
    extension: str                     # ".json" | ".safetensors" | ".onnx"
    model_name: str
    schema_version: str
    training_metrics: dict = field(default_factory=dict)   # training/validation-set numbers: NOT evidence for promotion


class Trainer(Protocol):
    name: str
    version: str

    def train(self, req: TrainingRequest) -> TrainingResult: ...
