"""Composition root. Everything environment-specific is constructed here and injected."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.session import make_engine, make_session_factory
from app.extraction.rules import RuleBasedExtractor
from app.ocr.providers import build_ocr_provider, build_retry_ocr_provider
from app.queue.base import CeleryJobQueue, JobQueue
from app.security.ratelimit import RateLimiter
from app.storage.base import ObjectStorage
from app.storage.factory import build_storage
from app.workers.job import WorkerContext


@dataclass
class Container:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    storage: ObjectStorage
    queue: JobQueue
    redis: object
    limiter: RateLimiter


def build_container(settings: Settings) -> Container:
    import redis as redis_lib
    r = redis_lib.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2, health_check_interval=30)
    engine = make_engine(settings)
    return Container(settings, engine, make_session_factory(engine), build_storage(settings),
                     CeleryJobQueue(r, settings), r, RateLimiter(r, fail_open=settings.rate_limit_fail_open))


def extractor_by_name(name: str):
    if name == "layout-kv":
        from app.extraction.layout_kv import LayoutKVExtractor
        return LayoutKVExtractor()
    if name == "layout-rules":
        from app.extraction.layout_rules import LayoutRulesExtractor
        return LayoutRulesExtractor()
    return RuleBasedExtractor()


def build_reference_extractor(settings: Settings):
    return extractor_by_name(settings.reference_extractor)


def build_worker_context(settings: Settings) -> WorkerContext:
    engine = make_engine(settings)
    return WorkerContext(settings, make_session_factory(engine), build_storage(settings),
                         build_ocr_provider(settings), build_reference_extractor(settings),
                         ocr_retry=build_retry_ocr_provider(settings))
