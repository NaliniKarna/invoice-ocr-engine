"""OpenTelemetry-compatible tracing. OFF by default and optional: nothing here may break business logic.

Enable in production: pip install -r requirements-otel.txt; set OCR_OTEL_ENABLED=true and standard env
OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS / OTEL_RESOURCE_ATTRIBUTES. Without the packages or with
tracing disabled, `span()` is a no-op. Span attributes never contain document content or credentials.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)
_tracer: Any = None
_propagator: Any = None


def setup_tracing(*, enabled: bool, service_name: str, provider: Any = None, app: Any = None, engine: Any = None) -> bool:
    """Returns True when tracing is active. `provider` lets tests inject an in-memory provider."""
    global _tracer, _propagator
    if not enabled and provider is None:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.propagate import get_global_textmap
        if provider is None:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            except ImportError:
                log.warning("OTLP exporter package missing; spans are created but not exported")
            trace.set_tracer_provider(provider)
        _tracer = provider.get_tracer("bill-ocr-api")
        _propagator = get_global_textmap()
        _optional_instrumentation(app, engine)
        return True
    except Exception:
        log.warning("tracing setup failed; continuing without tracing", exc_info=True)
        _tracer = None
        return False


def _optional_instrumentation(app: Any, engine: Any) -> None:
    try:
        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
        if engine is not None:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(engine=engine)
    except Exception:
        log.info("optional OTel instrumentation not applied")


def reset_for_tests() -> None:
    global _tracer, _propagator
    _tracer = _propagator = None


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[None]:
    cm = None
    if _tracer is not None:
        try:
            cm = _tracer.start_as_current_span(name, attributes={k: v for k, v in attrs.items() if v is not None})
            cm.__enter__()
        except Exception:
            cm = None
    try:
        yield
    except BaseException as exc:
        if cm is not None:
            try:
                cm.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
        raise
    else:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


def inject_headers() -> dict[str, str]:
    """W3C trace-context headers for the current span (empty when tracing is off)."""
    if _tracer is None or _propagator is None:
        return {}
    try:
        carrier: dict[str, str] = {}
        _propagator.inject(carrier)
        return carrier
    except Exception:
        return {}


@contextmanager
def continued(carrier: dict[str, str] | None, name: str, **attrs: Any) -> Iterator[None]:
    """Start `name` as a child of the context carried in `carrier` (worker side)."""
    token = None
    if _tracer is not None and carrier and _propagator is not None:
        try:
            from opentelemetry import context
            token = context.attach(_propagator.extract(carrier))
        except Exception:
            token = None
    try:
        with span(name, **attrs):
            yield
    finally:
        if token is not None:
            try:
                from opentelemetry import context
                context.detach(token)
            except Exception:
                pass
