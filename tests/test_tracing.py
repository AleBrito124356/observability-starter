"""Endpoints emit spans, and manual spans carry the right attributes and status."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export import ConsoleSpanExporter
from opentelemetry.trace import SpanKind

from app.config import Settings
from app.services import process_order
from app.telemetry.tracing import build_span_exporters

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_endpoint_creates_server_and_manual_spans(client, span_exporter):
    response = client.get("/api/orders/ORD-42?outcome=ok")
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    names = [s.name for s in spans]

    # FastAPI auto-instrumentation produces the SERVER span...
    assert any(s.kind == SpanKind.SERVER for s in spans)
    # ...and our business logic produces the manual spans.
    assert "process_order" in names
    assert "db.query" in names

    order_span = next(s for s in spans if s.name == "process_order")
    assert order_span.attributes.get("order.id") == "ORD-42"
    assert order_span.attributes.get("app.operation") == "process_order"


def test_db_span_is_child_of_process_order(client, span_exporter):
    client.get("/api/orders/ORD-7?outcome=ok")
    spans = {s.name: s for s in span_exporter.get_finished_spans()}

    process_span = spans["process_order"]
    db_span = spans["db.query"]
    assert db_span.parent is not None
    assert db_span.parent.span_id == process_span.context.span_id


def test_manual_span_records_error_on_failure(client, span_exporter):
    response = client.get("/api/orders/ORD-99?outcome=fail")
    assert response.status_code == 503

    order_span = next(
        s for s in span_exporter.get_finished_spans() if s.name == "process_order"
    )
    assert order_span.status.status_code.name == "ERROR"
    assert order_span.events, "expected a recorded exception event"


async def test_process_order_success_directly(span_exporter):
    result = await process_order("ORD-DIRECT", force_outcome="success")
    assert result["status"] == "confirmed"

    names = [s.name for s in span_exporter.get_finished_spans()]
    assert "process_order" in names
    assert "db.query" in names


# --- Regression tests: OTEL_TRACES_EXPORTER / OTEL_EXPORTER_OTLP_PROTOCOL -----


def _exporter_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_console_exporter_is_honoured():
    exporters = build_span_exporters(_exporter_settings(otel_traces_exporter="console"))
    assert [type(e) for e in exporters] == [ConsoleSpanExporter]


def test_none_builds_no_exporter():
    assert build_span_exporters(_exporter_settings(otel_traces_exporter="none")) == []


def test_comma_list_builds_every_exporter():
    exporters = build_span_exporters(_exporter_settings(otel_traces_exporter="otlp,console"))
    try:
        assert [type(e).__name__ for e in exporters] == ["OTLPSpanExporter", "ConsoleSpanExporter"]
        assert "grpc" in type(exporters[0]).__module__
    finally:
        for exporter in exporters:
            exporter.shutdown()


def test_http_protobuf_protocol_builds_the_http_exporter():
    settings = _exporter_settings(
        otel_traces_exporter="otlp",
        otel_exporter_otlp_protocol="http/protobuf",
        otel_exporter_otlp_endpoint="http://collector:4318",
    )
    (exporter,) = build_span_exporters(settings)
    try:
        assert "proto.http" in type(exporter).__module__
        assert exporter._endpoint == "http://collector:4318/v1/traces"
    finally:
        exporter.shutdown()


def test_unknown_exporter_raises_instead_of_falling_back_to_otlp():
    with pytest.raises(ValueError, match="unsupported exporter"):
        build_span_exporters(SimpleNamespace(otel_traces_exporter="bogus"))


def test_env_var_selects_console_exporter_end_to_end():
    # A fresh interpreter, so configure_tracing builds its own provider from
    # the environment exactly as `uvicorn app.main:app` would.
    code = (
        "from app.main import app\n"
        "p = app.state.tracer_provider\n"
        "print([type(sp.span_exporter).__name__"
        " for sp in p._active_span_processor._span_processors])\n"
    )
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("OTEL_")},
        "OTEL_TRACES_EXPORTER": "console",
        "LOG_LEVEL": "WARNING",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "['ConsoleSpanExporter']" in result.stdout
