"""Shared pytest fixtures.

The key trick: install a ``TracerProvider`` backed by an in-memory span
exporter *before* any app is built. ``configure_tracing`` reuses an existing
real provider instead of creating an OTLP one, so every span the app produces
lands in ``MEMORY_EXPORTER`` where the tests can inspect it - no collector
required. (Importing ``app.main`` builds nothing, so import order is free.)
"""

from __future__ import annotations

import io
import json
import logging
import os

# Belt and braces: even if a fresh provider were built, don't try to export.
os.environ.setdefault("OTEL_TRACES_EXPORTER", "none")

import pytest
import structlog
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.config import Settings
from app.main import create_app

MEMORY_EXPORTER = InMemorySpanExporter()

_provider = trace.get_tracer_provider()
if not isinstance(_provider, TracerProvider):
    _provider = TracerProvider(
        resource=Resource.create({"service.name": "observability-starter-tests"})
    )
    _provider.add_span_processor(SimpleSpanProcessor(MEMORY_EXPORTER))
    trace.set_tracer_provider(_provider)

_STDLIB_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def make_settings(**overrides) -> Settings:
    """Settings that ignore any local .env file, so tests are hermetic."""

    overrides.setdefault("otel_traces_exporter", "none")
    return Settings(_env_file=None, **overrides)


@pytest.fixture()
def app():
    # enable_logging=False so tests keep full control over structlog config.
    return create_app(
        settings=make_settings(),
        enable_tracing=True,
        enable_metrics=True,
        enable_logging=False,
    )


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def span_exporter():
    return MEMORY_EXPORTER


@pytest.fixture(autouse=True)
def _clear_spans():
    MEMORY_EXPORTER.clear()
    yield
    MEMORY_EXPORTER.clear()


@pytest.fixture()
def log_capture():
    """Run the *production* logging setup into a buffer, then undo it.

    Yields a function returning the JSON objects written so far. The global
    stdlib and structlog configuration is restored afterwards so other tests
    are unaffected.
    """

    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved_loggers = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
        for name in _STDLIB_LOGGERS
    }
    saved_structlog = structlog.get_config()
    buffer = io.StringIO()

    def read() -> list[dict]:
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    read.buffer = buffer
    try:
        yield read
    finally:
        root.handlers, level = saved_root
        root.setLevel(level)
        for name, (handlers, propagate) in saved_loggers.items():
            logging.getLogger(name).handlers = handlers
            logging.getLogger(name).propagate = propagate
        structlog.configure(**saved_structlog)
        structlog.contextvars.clear_contextvars()
