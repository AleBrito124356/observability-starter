"""Settings are validated at load time, with messages that name the variable."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, parse_exporter_list


def _settings(monkeypatch, **env) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_defaults_are_valid(monkeypatch):
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    s = Settings(_env_file=None)
    assert s.trace_sample_ratio == 1.0
    assert s.traces_exporters == ("otlp",)
    assert s.otel_exporter_otlp_protocol == "grpc"
    assert s.otlp_endpoint == "http://localhost:4317"


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"FAILURE_RATE": "7"}, "failure_rate"),
        ({"TRACE_SAMPLE_RATIO": "1.5"}, "trace_sample_ratio"),
        ({"TRACE_SAMPLE_RATIO": "-0.1"}, "trace_sample_ratio"),
        ({"SLOW_MIN_MS": "500", "SLOW_MAX_MS": "10"}, "SLOW_MIN_MS"),
        ({"SLOW_MIN_MS": "-5"}, "slow_min_ms"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
        ({"OTEL_TRACES_EXPORTER": "bogus"}, "OTEL_TRACES_EXPORTER"),
        ({"OTEL_TRACES_EXPORTER": "none,otlp"}, "cannot be combined"),
        ({"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}, "otel_exporter_otlp_protocol"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "collector:4317"}, "http://"),
    ],
)
def test_invalid_settings_fail_fast(monkeypatch, env, fragment):
    with pytest.raises(ValidationError) as excinfo:
        _settings(monkeypatch, **env)
    assert fragment in str(excinfo.value)


def test_log_level_is_normalised(monkeypatch):
    assert _settings(monkeypatch, LOG_LEVEL="warn").log_level == "WARNING"
    assert _settings(monkeypatch, LOG_LEVEL="debug").log_level_number == 10


def test_http_protocol_defaults_to_port_4318(monkeypatch):
    s = _settings(monkeypatch, OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf")
    assert s.otlp_endpoint == "http://localhost:4318"


def test_explicit_endpoint_wins_and_loses_trailing_slash(monkeypatch):
    s = _settings(monkeypatch, OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318/")
    assert s.otlp_endpoint == "http://collector:4318"


def test_parse_exporter_list():
    assert parse_exporter_list("none", variable="X") == ()
    assert parse_exporter_list(" OTLP , console ", variable="X") == ("otlp", "console")
    assert parse_exporter_list("otlp,otlp", variable="X") == ("otlp",)
    with pytest.raises(ValueError, match="unsupported exporter"):
        parse_exporter_list("zipkin", variable="X")
    with pytest.raises(ValueError, match="empty"):
        parse_exporter_list(" , ", variable="X")
