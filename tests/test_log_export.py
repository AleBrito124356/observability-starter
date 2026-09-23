"""OTLP log export: log records leave the process with their trace context."""

from __future__ import annotations

import logging

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor

from app.telemetry import setup_telemetry
from app.telemetry.logging import (
    OTelLogHandler,
    build_log_exporters,
    configure_log_export,
    configure_logging,
)
from tests.conftest import make_settings

try:  # renamed in opentelemetry-sdk 1.37
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as MemoryLogs
except ImportError:  # pragma: no cover - older SDKs
    from opentelemetry.sdk._logs.export import InMemoryLogExporter as MemoryLogs


def _records(exporter) -> list:
    return [item.log_record for item in exporter.get_finished_logs()]


def _resource(exporter):
    item = exporter.get_finished_logs()[0]
    return getattr(item, "resource", None) or item.log_record.resource


@pytest.fixture()
def memory_logs(log_capture):
    """An in-memory log exporter wired through ``setup_telemetry`` on a bare app."""

    exporter = MemoryLogs()
    app = FastAPI()
    log = structlog.get_logger("widgets")

    @app.get("/widgets/{widget_id}")
    async def read(widget_id: str) -> dict:
        log.info("widget.read", widget_id=widget_id, stock=3, tags=["a", "b"])
        logging.getLogger("uvicorn.error").warning("plain stdlib %s", "record")
        return {"id": widget_id}

    telemetry = setup_telemetry(
        app,
        make_settings(),
        log_stream=log_capture.buffer,
        log_processors=[SimpleLogRecordProcessor(exporter)],
    )
    yield exporter, TestClient(app), telemetry
    telemetry.shutdown()


def test_structlog_event_is_exported_with_its_trace_context(memory_logs, span_exporter):
    exporter, client, _ = memory_logs
    client.get("/widgets/w-9", headers={"X-Request-ID": "otlp-1"})

    server = next(s for s in span_exporter.get_finished_spans() if s.kind.name == "SERVER")
    record = next(r for r in _records(exporter) if r.body == "widget.read")
    assert record.trace_id == server.context.trace_id
    assert record.span_id == server.context.span_id
    assert record.severity_number == SeverityNumber.INFO
    assert record.severity_text == "INFO"
    attributes = dict(record.attributes)
    assert attributes["widget_id"] == "w-9"
    assert attributes["stock"] == 3
    assert attributes["request_id"] == "otlp-1"
    assert tuple(attributes["tags"]) == ("a", "b")
    # Record fields are not duplicated as attributes.
    for field in ("event", "level", "timestamp", "trace_id", "span_id"):
        assert field not in attributes
    assert _resource(exporter).attributes["service.name"] == "observability-starter"


def test_foreign_stdlib_record_gets_the_bound_request_id(memory_logs):
    exporter, client, _ = memory_logs
    client.get("/widgets/w-1", headers={"X-Request-ID": "otlp-2"})

    record = next(r for r in _records(exporter) if r.body == "plain stdlib record")
    assert record.severity_number == SeverityNumber.WARN
    assert dict(record.attributes)["request_id"] == "otlp-2"
    assert record.trace_id != 0


def test_access_log_is_exported_too(memory_logs):
    exporter, client, _ = memory_logs
    client.get("/widgets/w-2")
    record = next(r for r in _records(exporter) if r.body == "request.completed")
    assert dict(record.attributes)["route"] == "/widgets/{widget_id}"


def test_exception_info_becomes_semantic_attributes(log_capture):
    exporter = MemoryLogs()
    configure_logging(make_settings(), stream=log_capture.buffer)
    provider = configure_log_export(
        make_settings(), log_processors=[SimpleLogRecordProcessor(exporter)]
    )
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            structlog.get_logger("x").exception("it.failed")
        record = next(r for r in _records(exporter) if r.body == "it.failed")
        attributes = dict(record.attributes)
        assert record.severity_number == SeverityNumber.ERROR
        assert "ValueError: boom" in attributes["exception.stacktrace"]
        assert attributes["exception.type"] == "ValueError"
    finally:
        provider.shutdown()


def test_sdk_records_are_never_fed_back(log_capture):
    exporter = MemoryLogs()
    configure_logging(make_settings(), stream=log_capture.buffer)
    provider = configure_log_export(
        make_settings(), log_processors=[SimpleLogRecordProcessor(exporter)]
    )
    try:
        logging.getLogger("opentelemetry.exporter.otlp").warning("export failed")
        logging.getLogger("app.other").warning("kept")
        assert [r.body for r in _records(exporter)] == ["kept"]
    finally:
        provider.shutdown()


def test_reconfiguring_replaces_the_handler(log_capture):
    settings = make_settings()
    configure_logging(settings, stream=log_capture.buffer)
    first = configure_log_export(settings, log_processors=[SimpleLogRecordProcessor(MemoryLogs())])
    second = configure_log_export(settings, log_processors=[SimpleLogRecordProcessor(MemoryLogs())])
    handlers = [h for h in logging.getLogger().handlers if isinstance(h, OTelLogHandler)]
    assert [h.logger_provider for h in handlers] == [second]
    first.shutdown()
    second.shutdown()


def test_shutdown_detaches_the_handler(memory_logs):
    _, _, telemetry = memory_logs
    telemetry.shutdown()
    assert not [h for h in logging.getLogger().handlers if isinstance(h, OTelLogHandler)]


def test_logs_export_is_off_by_default(log_capture):
    assert make_settings().logs_exporters == ()
    assert configure_log_export(make_settings()) is None
    assert not [h for h in logging.getLogger().handlers if isinstance(h, OTelLogHandler)]


def test_log_exporter_selection():
    (console,) = build_log_exporters(make_settings(otel_logs_exporter="console"))
    assert "Console" in type(console).__name__

    (http,) = build_log_exporters(
        make_settings(
            otel_logs_exporter="otlp",
            otel_exporter_otlp_protocol="http/protobuf",
            otel_exporter_otlp_endpoint="http://collector:4318",
        )
    )
    assert "proto.http" in type(http).__module__
    assert http._endpoint == "http://collector:4318/v1/logs"

    (grpc,) = build_log_exporters(make_settings(otel_logs_exporter="otlp"))
    assert "proto.grpc" in type(grpc).__module__
    for exporter in (http, grpc):
        exporter.shutdown()


def test_log_outside_a_span_has_no_trace(log_capture):
    exporter = MemoryLogs()
    configure_logging(make_settings(), stream=log_capture.buffer)
    provider = configure_log_export(
        make_settings(), log_processors=[SimpleLogRecordProcessor(exporter)]
    )
    try:
        assert not trace.get_current_span().get_span_context().is_valid
        structlog.get_logger("x").info("no.span")
        record = _records(exporter)[-1]
        assert not record.trace_id
    finally:
        provider.shutdown()
