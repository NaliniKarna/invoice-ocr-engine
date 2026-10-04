"""Operator CLI (same capabilities as the /internal/v1 ML endpoints, for batch use without an HTTP hop).

  python -m app.ml.cli build-dataset --scope pooled --kind training --seed 1
  python -m app.ml.cli build-dataset --scope tenant:cli_xxx --kind evaluation --seed 1
  python -m app.ml.cli models

Training, evaluation on real data and promotion are deliberately NOT one-shot commands: each is a separate, audited step
(see docs/ML_PIPELINE.md). `benchmarks.run` produces the synthetic benchmark report.
"""
import argparse
import json

from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.ml.dataset import build_dataset
from app.ml.splits import SplitSpec
from app.models import ModelVersion
from app.storage.factory import build_storage


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-dataset")
    b.add_argument("--scope", required=True, help="'pooled' or 'tenant:<client_id>'")
    b.add_argument("--kind", choices=["training", "evaluation"], default="training")
    b.add_argument("--seed", default="0")
    b.add_argument("--min-examples", type=int, default=1)
    b.add_argument("--salt", default="")
    b.add_argument("--allow-small-eval", action="store_true")
    sub.add_parser("models")
    a = ap.parse_args()
    s = get_settings()
    with make_session_factory(make_engine(s))() as db:
        if a.cmd == "build-dataset":
            ds = build_dataset(db, build_storage(s), s, scope=a.scope, kind=a.kind, spec=SplitSpec(seed=a.seed),
                               min_examples=a.min_examples, pseudonymize_salt=a.salt, allow_small_eval=a.allow_small_eval,
                               created_by="cli")
            if ds is None:
                print("no dataset created (below minimum size, no consenting tenants, or evaluation set too small)")
                return
            db.commit()
            print(f"dataset {ds.id}: {ds.record_count} records, scope={ds.scope}, kind={ds.kind}\n"
                  f"{json.dumps(ds.quality_report, indent=1, default=str)}")
        else:
            for mv in db.query(ModelVersion).order_by(ModelVersion.kind, ModelVersion.created_at):
                print(f"{mv.id}  {mv.kind:10} {mv.name}@{mv.version}  {mv.status}")


if __name__ == "__main__":
    main()
