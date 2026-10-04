"""Create a client + first API key from the command line (ops use; prints the key ONCE).
   python -m scripts.bootstrap_client "Acme Ltd" --plan pro"""
import argparse

from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.services.admin import AdminService


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name"); ap.add_argument("--plan", default="free"); ap.add_argument("--opt-in-training", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    with make_session_factory(make_engine(s))() as db:
        svc = AdminService(db, s)
        c = svc.create_client(name=a.name, plan_id=a.plan, training_opt_in=a.opt_in_training)
        k, full = svc.create_key(c.id, name="initial")
    print(f"client_id: {c.id}\nkey_id:    {k.id}\napi_key:   {full}\n(store the key now; it cannot be shown again)")


if __name__ == "__main__":
    main()
