"""Per-worker model runtime: resolves the PRODUCTION extractor from the registry, loads it once, and falls back to the
reference extractor on ANY problem (no production model, unknown model type, bad artifact, incompatible schema, load error).

* The registry is polled at most every `refresh_seconds`; a worker never queries it per document.
* A load failure is sticky for that model id until the pointer changes (no retry storm), is counted in metrics, and is
  visible through `status()` (used by readiness). The job is still served by the reference extractor.
* Loaded extractors are cached by model id; a pointer change swaps atomically, in-flight jobs keep the object they hold.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.extraction.base import Extractor
from app.extraction.schema import SUPPORTED_SCHEMA_VERSIONS
from app.ml import artifacts
from app.models import ModelVersion
from app.observability import metrics as m
from app.repositories.model_versions import ModelVersionRepository
from app.storage.base import ObjectStorage

log = logging.getLogger(__name__)

# name -> factory(artifact_bytes | None, config) -> Extractor.  Built-ins need no artifact; trained models register here.
ExtractorFactory = Callable[[bytes | None, dict], Extractor]
_FACTORIES: dict[str, ExtractorFactory] = {}


def register_extractor_factory(name: str, factory: ExtractorFactory) -> None:
    _FACTORIES[name] = factory


def _builtin_factories() -> None:
    from app.extraction.layout_rules import LayoutRulesExtractor
    from app.extraction.rules import RuleBasedExtractor
    _FACTORIES.setdefault("rule-based-baseline", lambda blob, cfg: RuleBasedExtractor())
    _FACTORIES.setdefault("layout-rules", lambda blob, cfg: LayoutRulesExtractor())


class ModelRuntime:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], storage: ObjectStorage,
                 reference: Extractor, *, refresh_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic):
        _builtin_factories()
        self.settings, self.sf, self.storage, self.reference = settings, session_factory, storage, reference
        self.refresh_seconds, self._clock = refresh_seconds, clock
        self._lock = threading.Lock()
        self._current: Extractor = reference
        self._current_id: str | None = None
        self._failed_ids: dict[str, str] = {}
        self._checked_at = -1e18
        self._last_error: str | None = None

    def current(self) -> Extractor:
        now = self._clock()
        if now - self._checked_at >= self.refresh_seconds:
            with self._lock:
                if now - self._checked_at >= self.refresh_seconds:
                    self._checked_at = now
                    self._refresh()
        return self._current

    def _refresh(self) -> None:
        try:
            with self.sf() as db:
                prod = ModelVersionRepository(db).active("extractor")
                if prod is None:
                    self._use_reference(None, None)
                    return
                if prod.id == self._current_id:
                    return
                if prod.id in self._failed_ids:
                    m.ML_FALLBACK_ACTIVE.labels(reason=self._failed_ids[prod.id]).inc()
                    return
                self._load(prod)
        except Exception:          # registry unreachable: keep serving with whatever is loaded
            log.warning("model registry check failed", exc_info=False)
            self._last_error = "registry_unreachable"

    def _use_reference(self, model_id: str | None, reason: str | None) -> None:
        self._current, self._current_id, self._last_error = self.reference, model_id, reason
        if reason:
            m.ML_FALLBACK_ACTIVE.labels(reason=reason).inc()

    def _load(self, mv: ModelVersion) -> None:
        t0 = time.perf_counter()
        try:
            if mv.schema_version and mv.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                raise artifacts.ArtifactError("schema_incompatible")
            factory = _FACTORIES.get(mv.name)
            if factory is None:
                raise artifacts.ArtifactError("unknown_model_type")
            blob = None
            if mv.artifact_uri:
                artifacts.check_uri(mv.artifact_uri)       # location and format are checked BEFORE any read
                blob = self.storage.get(mv.artifact_uri)
                artifacts.verify(blob, uri=mv.artifact_uri, expected_sha256=mv.artifact_sha256, signature=mv.signature,
                                 key=self.settings.ml_model_signing_key.get_secret_value().encode())
            ex = factory(blob, dict(mv.config or {}))
            self._current, self._current_id, self._last_error = ex, mv.id, None
            m.ML_MODEL_LOAD.labels(kind="extractor").observe(time.perf_counter() - t0)
            log.info("extractor model loaded", extra={"model": f"{ex.name}@{ex.version}"})
        except artifacts.ArtifactError as e:
            self._fail(mv, e.reason)
        except Exception:
            self._fail(mv, "load_error")

    def _fail(self, mv: ModelVersion, reason: str) -> None:
        self._failed_ids[mv.id] = reason
        m.ML_MODEL_LOAD_ERRORS.labels(kind="extractor", reason=reason).inc()
        log.error("extractor model rejected; using reference extractor", extra={"reason": reason, "model_id": mv.id})
        self._use_reference(mv.id, reason)

    def status(self) -> dict:
        """Readiness/diagnostics. Never contains paths, secrets or document data."""
        return {"extractor": f"{self._current.name}@{self._current.version}", "model_id": self._current_id,
                "using_reference": self._current is self.reference, "last_error": self._last_error}
