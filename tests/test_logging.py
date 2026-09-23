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
        processors=[*shared_processors(get_settings()), sink],
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


# --- The structured access log -------------------------------------------------


def _app_logging_to(buffer, **overrides):
    from app.main import create_app
    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    settings = make_settings(**overrides)
    configure_logging(settings, stream=buffer)
    return create_app(settings=settings, enable_logging=False)


def test_access_log_line_is_structured_and_correlated(log_capture, span_exporter):
    from fastapi.testclient import TestClient

    client = TestClient(_app_logging_to(log_capture.buffer))
    client.get("/api/orders/ORD-77?outcome=ok", headers={"X-Request-ID": "acc-1"})
    client.get("/health")
    client.get("/metrics")

    access = [e for e in log_capture() if e.get("event") == "request.completed"]
    assert len(access) == 1, "probes and scrapes are not access-logged"
    line = access[0]
    server = next(s for s in span_exporter.get_finished_spans() if s.kind.name == "SERVER")
    assert line["route"] == "/api/orders/{order_id}"
    assert line["path"] == "/api/orders/ORD-77"
    assert line["method"] == "GET"
    assert line["status_code"] == 200
    assert line["duration_ms"] > 0
    assert line["request_id"] == "acc-1"
    assert line["trace_id"] == format(server.context.trace_id, "032x")


def test_access_log_marks_5xx_as_warning(log_capture):
    from fastapi.testclient import TestClient

    TestClient(_app_logging_to(log_capture.buffer)).get("/api/orders/ORD-1?outcome=fail")
    line = next(e for e in log_capture() if e.get("event") == "request.completed")
    assert line["status_code"] == 503
    assert line["level"] == "warning"


def test_access_log_can_be_turned_off(log_capture):
    from fastapi.testclient import TestClient

    TestClient(_app_logging_to(log_capture.buffer, log_requests=False)).get("/")
    assert not [e for e in log_capture() if e.get("event") == "request.completed"]


async def test_nested_in_process_request_keeps_the_outer_request_id(log_capture):
    import httpx

    from app.demo.runner import NetworkLikeASGITransport

    app = _app_logging_to(log_capture.buffer, upstream_url="http://upstream.internal/")
    # A plain ASGITransport shares the caller's context: the worst case for
    # context-local state such as the bound request id.
    app.state.http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    async with httpx.AsyncClient(
        transport=NetworkLikeASGITransport(app=app), base_url="http://svc"
    ) as client:
        await client.get("/api/external", headers={"X-Request-ID": "outer-1"})
    await app.state.http_client.aclose()

    lines = {e["path"]: e for e in log_capture() if e.get("event") == "request.completed"}
    assert lines["/api/external"]["request_id"] == "outer-1"
    assert lines["/"]["request_id"] != "outer-1"


# --- configure_logging, end to end on stdout ----------------------------------


def test_configure_logging_writes_one_json_object_per_line(log_capture, capsys):
    import json
    import logging

    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    configure_logging(make_settings())  # default stream: sys.stdout
    log = structlog.get_logger("stdout-test")
    tracer = trace.get_tracer("test")
    with structlog.contextvars.bound_contextvars(request_id="req-json-1"):
        with tracer.start_as_current_span("unit") as span:
            ctx = span.get_span_context()
            log.info("structlog.event", answer=42)
            logging.getLogger("uvicorn.error").warning("uvicorn says %s", "hi")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]  # every line is a JSON object
    by_event = {entry["event"]: entry for entry in entries}
    for event in ("structlog.event", "uvicorn says hi"):
        entry = by_event[event]
        assert entry["trace_id"] == format(ctx.trace_id, "032x")
        assert entry["span_id"] == format(ctx.span_id, "016x")
        assert entry["request_id"] == "req-json-1"
        assert "timestamp" in entry
    assert by_event["structlog.event"]["answer"] == 42
    assert by_event["uvicorn says hi"]["level"] == "warning"


def _uvicorn_after_dictconfig():
    import logging

    # What uvicorn's LOGGING_CONFIG leaves behind: own handlers, no propagation.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = [logging.NullHandler()]
        logging.getLogger(name).propagate = name != "uvicorn.access"


def test_uvicorn_loggers_propagate_to_the_json_handler(log_capture):
    import logging

    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    _uvicorn_after_dictconfig()
    configure_logging(make_settings(), stream=log_capture.buffer)
    for name in ("uvicorn", "uvicorn.error"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    assert log_capture()[-1]["event"] == "Application startup complete."


def test_uvicorn_access_log_is_silenced_while_the_app_logs_requests(log_capture):
    import logging

    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    _uvicorn_after_dictconfig()
    configure_logging(make_settings(), stream=log_capture.buffer)
    access = logging.getLogger("uvicorn.access")
    # uvicorn checks hasHandlers() once per connection and skips access lines.
    assert access.hasHandlers() is False
    access.info("GET / 200")
    assert log_capture() == []


def test_uvicorn_access_log_is_json_when_request_logging_is_off(log_capture):
    import logging

    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    _uvicorn_after_dictconfig()
    configure_logging(make_settings(log_requests=False), stream=log_capture.buffer)
    access = logging.getLogger("uvicorn.access")
    assert access.hasHandlers() is True
    access.info("GET / 200")
    assert log_capture()[-1]["event"] == "GET / 200"


def test_log_level_setting_filters_records(log_capture):
    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    configure_logging(make_settings(log_level="WARNING"), stream=log_capture.buffer)
    log = structlog.get_logger("levels")
    log.info("dropped")
    log.warning("kept")
    assert [entry["event"] for entry in log_capture()] == ["kept"]


def test_console_renderer_when_json_is_off(log_capture):
    from app.telemetry.logging import configure_logging
    from tests.conftest import make_settings

    configure_logging(make_settings(log_json=False), stream=log_capture.buffer)
    structlog.get_logger("pretty").info("human.readable", k="v")
    text = log_capture.buffer.getvalue()
    assert "human.readable" in text
    assert not text.lstrip().startswith("{")
