from __future__ import annotations

import os

os.environ.setdefault("OCR_API_KEY_PEPPER", "test-pepper-" + "x" * 32)
os.environ.setdefault("OCR_ADMIN_TOKEN", "test-admin-token-" + "y" * 32)

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.container import Container
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.extraction.rules import RuleBasedExtractor
from app.main import create_app
from app.models import Plan
from app.security.ratelimit import RateLimiter
from app.services.admin import AdminService
from app.storage.local import LocalFsStorage
from app.workers.job import WorkerContext, run_job
from tests.fakes import FakeOCR, InMemoryQueue

ADMIN = {"X-Admin-Token": os.environ["OCR_ADMIN_TOKEN"]}


def _plans():
    big = dict(requests_per_minute=1000, tenant_requests_per_minute=2000, upload_requests_per_minute=500,
               max_concurrent_jobs=1000, daily_quota=100000, monthly_quota=1000000,
               max_file_bytes=10 * 1024 * 1024, max_batch_size=50, max_batch_bytes=100 * 1024 * 1024, retention_days=30)
    small = dict(big, requests_per_minute=5, tenant_requests_per_minute=8, upload_requests_per_minute=3,
                 max_concurrent_jobs=4, daily_quota=6, monthly_quota=100, max_batch_size=5,
                 max_file_bytes=200 * 1024, max_batch_bytes=500 * 1024)
    return [Plan(id="enterprise", **big), Plan(id="free", **small)]


PG_URL = os.environ.get("OCR_TEST_DATABASE_URL")     # e.g. postgresql+psycopg://ocr:ocr@localhost:5433/ocr
REDIS_URL = os.environ.get("OCR_TEST_REDIS_URL")     # e.g. redis://localhost:6380/15 (DB is FLUSHED)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(env="test", database_url=PG_URL or "sqlite://", storage_backend="local", local_storage_dir=str(tmp_path / "obj"),
                    redis_url="redis://unused", max_pdf_pages=5, ip_requests_per_minute=10_000,
                    auth_failures_per_minute=5)


@pytest.fixture
def container(settings) -> Container:
    engine = make_engine(settings)
    if PG_URL:   # real PostgreSQL: build the schema through the real Alembic migration
        from alembic import command
        from alembic.config import Config
        with engine.begin() as c:
            c.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        os.environ["OCR_DATABASE_URL"] = PG_URL
        command.upgrade(Config("alembic.ini"), "head")
    else:
        Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    with sf() as db:
        for p in _plans():
            db.merge(p)   # test plans override the migration's seeded ones
        db.commit()
    if REDIS_URL:
        import redis as redis_lib
        redis = redis_lib.Redis.from_url(REDIS_URL); redis.flushdb()
    else:
        redis = fakeredis.FakeRedis()
    return Container(settings, engine, sf, LocalFsStorage(settings.local_storage_dir), InMemoryQueue(), redis,
                     RateLimiter(redis))


@pytest.fixture
def queue(container) -> InMemoryQueue:
    return container.queue  # type: ignore[return-value]


@pytest.fixture
def client(container) -> TestClient:
    return TestClient(create_app(container), raise_server_exceptions=False)


class Tenant:
    def __init__(self, client_id, key_id, api_key):
        self.client_id, self.key_id, self.api_key = client_id, key_id, api_key

    @property
    def h(self):
        return {"X-API-Key": self.api_key}


def make_tenant(container, name="acme", plan="enterprise", scopes=None, opt_in=False, **kw) -> Tenant:
    with container.session_factory() as db:
        svc = AdminService(db, container.settings)
        c = svc.create_client(name=name, plan_id=plan, training_opt_in=opt_in)
        k, full = svc.create_key(c.id, name="default", scopes=scopes, **kw)
        return Tenant(c.id, k.id, full)


@pytest.fixture
def tenant_a(container): return make_tenant(container, "tenant-a")


@pytest.fixture
def tenant_b(container): return make_tenant(container, "tenant-b")


@pytest.fixture
def ocr() -> FakeOCR:
    return FakeOCR()


@pytest.fixture
def worker_ctx(container, ocr) -> WorkerContext:
    return WorkerContext(container.settings, container.session_factory, container.storage, ocr, RuleBasedExtractor())


@pytest.fixture
def run_queue(worker_ctx, queue):
    def _run():
        results = []
        while queue.items:
            results.append(run_job(queue.items.pop(0), worker_ctx))
        return results
    return _run
