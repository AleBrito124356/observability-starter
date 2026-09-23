"""``setup_telemetry`` is a copy-and-go API: one call on a bare FastAPI app."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY

from app.telemetry import Telemetry, setup_telemetry
from tests.conftest import make_settings

TELEMETRY_DIR = Path(__file__).resolve().parents[1] / "app" / "telemetry"


def test_setup_telemetry_on_a_bare_app_gives_all_three_pillars(span_exporter, log_capture):
    import structlog

    bare = FastAPI()
    log = structlog.get_logger("bare")

    @bare.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str) -> dict:
        log.info("widget.read", widget_id=widget_id)
        return {"id": widget_id}

    telemetry = setup_telemetry(bare, make_settings(), log_stream=log_capture.buffer)
    assert isinstance(telemetry, Telemetry)
    assert bare.state.telemetry is telemetry

    labels = {"method": "GET", "path": "/widgets/{widget_id}", "status_code": "200"}
    before = REGISTRY.get_sample_value("http_requests_total", labels) or 0.0

    client = TestClient(bare)
    response = client.get("/widgets/w-1", headers={"X-Request-ID": "bare-req-1"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "bare-req-1"

    # Metrics: RED series under the route template, served at /metrics.
    assert REGISTRY.get_sample_value("http_requests_total", labels) == before + 1.0
    assert client.get("/metrics", follow_redirects=False).status_code == 200

    # Traces: the auto-instrumented server span.
    server = next(s for s in span_exporter.get_finished_spans() if s.kind == SpanKind.SERVER)
    trace_id = format(server.context.trace_id, "032x")

    # Logs: the app's own log line, correlated with that span and request.
    line = next(entry for entry in log_capture() if entry.get("event") == "widget.read")
    assert line["trace_id"] == trace_id
    assert line["request_id"] == "bare-req-1"
    assert line["widget_id"] == "w-1"


def test_setup_telemetry_can_skip_pillars():
    bare = FastAPI()
    telemetry = setup_telemetry(bare, make_settings(), logging=False, metrics=False, tracing=False)
    assert telemetry.tracer_provider is None
    assert TestClient(bare).get("/metrics").status_code == 404


def test_telemetry_package_is_self_contained():
    # No absolute `app...` imports at all (only relative ones inside the
    # package), so the folder works when copied anywhere under any name.
    offenders = []
    for path in TELEMETRY_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            offenders += [(path.name, n) for n in names if n == "app" or n.startswith("app.")]
    assert offenders == []


def test_telemetry_package_works_when_copied_under_another_name(tmp_path):
    import shutil
    import subprocess
    import sys

    shutil.copytree(TELEMETRY_DIR, tmp_path / "mytelemetry")
    code = (
        "from types import SimpleNamespace\n"
        "from fastapi import FastAPI\n"
        "from fastapi.testclient import TestClient\n"
        "from mytelemetry import setup_telemetry\n"
        "settings = SimpleNamespace(service_name='copied', service_version='1', environment='t',"
        " otel_traces_exporter='none', otel_logs_exporter='none', trace_sample_ratio=1.0,"
        " log_level='INFO', log_json=True, log_requests=True)\n"
        "app = FastAPI()\n"
        "@app.get('/ping')\n"
        "async def ping():\n"
        "    return {'ok': True}\n"
        "setup_telemetry(app, settings)\n"
        "client = TestClient(app)\n"
        "assert client.get('/ping').status_code == 200\n"
        "assert 'http_requests_total' in client.get('/metrics').text\n"
        "print('copied-ok')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,  # the repo's `app` package is not importable from here
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "copied-ok" in result.stdout


def test_importing_app_main_has_no_side_effects():
    import app.main as main_module

    # `app` is created lazily on first attribute access (PEP 562), so merely
    # importing the module (tests, demo, CLI) builds nothing.
    assert "app" not in vars(main_module)
