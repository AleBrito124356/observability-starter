"""Log/trace correlation: log lines carry the active trace and span ids."""

from __future__ import annotations

import structlog
from opentelemetry import trace

from app.config import get_settings
from app.telemetry.logging import add_trace_context, shared_processors


def _capture_logs():
    """Configure structlog with the production chain plus a capturing sink."""

    captured: list[dict] = []

    def sink(logger, method_name, event_dict):
        captured.append(event_dict)
        raise structlog.DropEvent

    structlog.configure(
        processors=shared_processors(get_settings()) + [sink],
        cache_logger_on_first_use=False,
    )
    return captured


def test_logs_carry_trace_and_span_id():
    captured = _capture_logs()
    try:
        log = structlog.get_logger("test")
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("unit-span") as span:
            ctx = span.get_span_context()
            log.info("event.happened", key="value")
    finally:
        structlog.reset_defaults()

    assert captured, "no log line was captured"
    entry = captured[-1]
    assert entry["event"] == "event.happened"
    assert entry["key"] == "value"
    assert entry["trace_id"] == format(ctx.trace_id, "032x")
    assert entry["span_id"] == format(ctx.span_id, "016x")


def test_no_trace_context_outside_a_span():
    captured = _capture_logs()
    try:
        log = structlog.get_logger("test")
        log.info("no.span.here")
    finally:
        structlog.reset_defaults()

    entry = captured[-1]
    # Outside any span there is no valid context, so no ids are injected.
    assert "trace_id" not in entry
    assert "span_id" not in entry


def test_add_trace_context_is_a_noop_without_a_span():
    event_dict = add_trace_context(None, "info", {"event": "x"})
    assert "trace_id" not in event_dict


# --- Regression tests: incoming X-Request-ID is validated --------------------


def test_valid_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "req-2026.07:abc_DEF"})
    assert response.headers["x-request-id"] == "req-2026.07:abc_DEF"


def test_missing_request_id_is_generated(client):
    rid = client.get("/health").headers["x-request-id"]
    assert len(rid) == 32 and all(c in "0123456789abcdef" for c in rid)


def test_oversized_request_id_is_replaced(client):
    rid = client.get("/health", headers={"X-Request-ID": "A" * 6000}).headers["x-request-id"]
    assert len(rid) == 32 and "A" not in rid


def test_request_id_with_injection_payload_is_replaced(client):
    evil = 'evil value with spaces {"level":"error"}'
    rid = client.get("/health", headers={"X-Request-ID": evil}).headers["x-request-id"]
    assert rid != evil
    assert len(rid) == 32


def test_request_id_lands_on_the_server_span(client, span_exporter):
    client.get("/api/orders/ORD-5?outcome=ok", headers={"X-Request-ID": "trace-me-123"})
    server = next(s for s in span_exporter.get_finished_spans() if s.kind.name == "SERVER")
    assert server.attributes.get("request.id") == "trace-me-123"
