"""Migration 0004 keeps the meaning of existing data: opt-in tenants become 'pooled', registry statuses are renamed."""
import os
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from tests.conftest import PG_URL

needs_pg = pytest.mark.skipif(not PG_URL, reason="needs PostgreSQL")


@needs_pg
def test_upgrade_preserves_consent_and_renames_statuses_and_downgrade_restores():
    from alembic import command
    from alembic.config import Config
    admin = sa.create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    name = "mig4_" + uuid.uuid4().hex[:8]
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = PG_URL.rsplit("/", 1)[0] + "/" + name
    old = os.environ.get("OCR_DATABASE_URL")
    os.environ["OCR_DATABASE_URL"] = url
    try:
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "0003")
        eng = sa.create_engine(url)
        with eng.begin() as c:
            plan = c.execute(text("SELECT id FROM plans LIMIT 1")).scalar()
            for cid, opt in (("cli_in", True), ("cli_out", False)):
                c.execute(text("INSERT INTO clients (id, name, plan_id, status, training_opt_in, created_at) "
                               "VALUES (:i, 'x', :p, 'active', :o, now())"), {"i": cid, "p": plan, "o": opt})
            for mid, st in (("m_act", "active"), ("m_eval", "evaluating"), ("m_cand", "candidate")):
                c.execute(text("INSERT INTO model_versions (id, kind, name, version, status, metrics, created_at, updated_at) "
                               "VALUES (:i, 'extractor', :n, '1', :s, '{}', now(), now())"), {"i": mid, "n": mid, "s": st})
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            pol = dict(c.execute(text("SELECT id, training_policy FROM clients WHERE id IN ('cli_in','cli_out')")).all())
            assert pol == {"cli_in": "pooled", "cli_out": "none"}
            st = dict(c.execute(text("SELECT id, status FROM model_versions")).all())
            assert st == {"m_act": "production", "m_eval": "evaluated", "m_cand": "candidate"}
            length = c.execute(text("SELECT character_maximum_length FROM information_schema.columns "
                                    "WHERE table_name='ocr_results' AND column_name='preprocessing_version'")).scalar()
            assert length == 64
        with eng.begin() as c:
            c.execute(text("INSERT INTO evaluation_runs (id, model_version_id, dataset_ref, dataset_sha256, split, sample_count, "
                           "metrics, slices, config, environment, synthetic, result_sha256) VALUES "
                           "('e2','m_act','r','s','test',1,'{}','{}','{}','{}',false,'h')"))
        with pytest.raises(Exception):
            with eng.begin() as c:
                c.execute(text("UPDATE evaluation_runs SET sample_count=99 WHERE id='e2'"))
        with pytest.raises(Exception):
            with eng.begin() as c:
                c.execute(text("DELETE FROM evaluation_runs WHERE id='e2'"))
        command.downgrade(cfg, "0003")
        with eng.connect() as c:
            st = dict(c.execute(text("SELECT id, status FROM model_versions")).all())
            assert st == {"m_act": "active", "m_eval": "evaluating", "m_cand": "candidate"}
        eng.dispose()
    finally:
        if old is not None:
            os.environ["OCR_DATABASE_URL"] = old
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
