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


# --- Context propagation --------------------------------------------------------


def test_background_task_span_joins_the_request_trace(client, span_exporter):
    response = client.post("/api/orders", json={"items": [{"sku": "SKU-1"}]})
    assert response.status_code == 202

    spans = span_exporter.get_finished_spans()
    server = next(s for s in spans if s.kind == SpanKind.SERVER)
    confirmation = next(s for s in spans if s.name == "send_confirmation")
    # The background task runs after the response, but joins the same trace,
    # parented under the request's server span.
    assert confirmation.kind == SpanKind.PRODUCER
    assert confirmation.context.trace_id == server.context.trace_id
    assert confirmation.parent.span_id == server.context.span_id


def _external_app(handler):
    import httpx
    from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport

    from app.main import create_app
    from tests.conftest import make_settings

    upstream = httpx.AsyncClient(
        transport=AsyncOpenTelemetryTransport(httpx.MockTransport(handler))
    )
    app = create_app(
        settings=make_settings(upstream_url="http://inventory.internal/stock"),
        enable_logging=False,
        http_client=upstream,
    )
    return app


def test_external_call_propagates_traceparent_from_a_client_span(span_exporter):
    import httpx
    from fastapi.testclient import TestClient

    seen: dict[str, str] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    response = TestClient(_external_app(upstream)).get("/api/external")
    assert response.status_code == 200
    assert response.json()["upstream_status"] == 200

    spans = span_exporter.get_finished_spans()
    client_span = next(s for s in spans if s.kind == SpanKind.CLIENT and s.name.startswith("GET"))
    aggregate = next(s for s in spans if s.name == "external.aggregate")
    version, trace_id, parent_id, flags = seen["traceparent"].split("-")
    assert trace_id == format(client_span.context.trace_id, "032x")
    assert parent_id == format(client_span.context.span_id, "016x")
    assert int(flags, 16) & 0x01, "the sampled flag must be propagated"
    assert client_span.parent.span_id == aggregate.context.span_id
    assert aggregate.attributes["upstream.status_code"] == 200


def test_external_upstream_failure_is_a_502_with_one_recorded_error(span_exporter):
    import httpx
    from fastapi.testclient import TestClient

    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    response = TestClient(_external_app(upstream)).get("/api/external")
    assert response.status_code == 502

    aggregate = next(s for s in span_exporter.get_finished_spans() if s.name == "external.aggregate")
    assert aggregate.status.status_code.name == "ERROR"
    assert aggregate.status.description == "upstream request failed"
    exceptions = [e for e in aggregate.events if e.name == "exception"]
    assert len(exceptions) == 1
    assert exceptions[0].attributes["exception.type"].endswith("ConnectError")


def test_lifespan_owns_a_shared_upstream_client():
    import httpx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    app = create_app(settings=make_settings(), enable_logging=False)
    with TestClient(app):
        client = app.state.http_client
        assert isinstance(client, httpx.AsyncClient)
        assert not client.is_closed
    assert client.is_closed
    assert app.state.http_client is None


# --- Sampling -------------------------------------------------------------------


def test_sampler_honours_the_ratio_and_the_parent():
    from opentelemetry.sdk.trace.sampling import Decision
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        set_span_in_context,
    )

    from app.telemetry.tracing import build_sampler
    from tests.conftest import make_settings

    trace_id = 0x0AF7651916CD43DD8448EB211C80319C

    def decide(ratio: float, parent_sampled: bool | None) -> Decision:
        sampler = build_sampler(make_settings(trace_sample_ratio=ratio))
        context = None
        if parent_sampled is not None:
            flags = TraceFlags(TraceFlags.SAMPLED if parent_sampled else TraceFlags.DEFAULT)
            parent = SpanContext(trace_id, 0xB7AD6B7169203331, is_remote=True, trace_flags=flags)
            context = set_span_in_context(NonRecordingSpan(parent))
        return sampler.should_sample(context, trace_id, "root").decision

    assert decide(1.0, None) == Decision.RECORD_AND_SAMPLE
    assert decide(0.0, None) == Decision.DROP
    # ParentBased: the parent's decision wins over the ratio, in both directions.
    assert decide(0.0, True) == Decision.RECORD_AND_SAMPLE
    assert decide(1.0, False) == Decision.DROP


def test_env_ratio_reaches_the_provider_sampler():
    code = (
        "from app.main import app\n"
        "print(app.state.tracer_provider.sampler.get_description())\n"
    )
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("OTEL_")},
        "OTEL_TRACES_EXPORTER": "none",
        "TRACE_SAMPLE_RATIO": "0.25",
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
    assert "TraceIdRatioBased{0.25}" in result.stdout
