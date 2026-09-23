"""End to end over real OTLP: spans *and* logs leave the app with matching ids.

A fake collector (a stdlib HTTP server for ``http/protobuf``, a grpcio server
for ``grpc``) listens on an ephemeral 127.0.0.1 port and decodes what it
receives with the official ``opentelemetry-proto`` messages. The app runs in a
fresh interpreter configured only through environment variables - the same
path ``uvicorn app.main:app`` takes - serves one failing order, and flushes on
shutdown. Nothing leaves the machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent import futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

REPO_ROOT = Path(__file__).resolve().parents[1]

APP_SCRIPT = """
from fastapi.testclient import TestClient
from app.main import create_app

app = create_app()
with TestClient(app) as client:  # runs the lifespan, whose shutdown flushes
    response = client.get("/api/orders/ORD-E2E?outcome=fail", headers={"X-Request-ID": "e2e-1"})
print(response.status_code)
"""


class Received:
    def __init__(self) -> None:
        self.spans: list = []
        self.logs: list = []
        self.resources: list = []
        self.lock = threading.Lock()

    def add_traces(self, request: trace_service_pb2.ExportTraceServiceRequest) -> None:
        with self.lock:
            for resource_spans in request.resource_spans:
                self.resources.append(resource_spans.resource)
                for scope_spans in resource_spans.scope_spans:
                    self.spans.extend(scope_spans.spans)

    def add_logs(self, request: logs_service_pb2.ExportLogsServiceRequest) -> None:
        with self.lock:
            for resource_logs in request.resource_logs:
                self.resources.append(resource_logs.resource)
                for scope_logs in resource_logs.scope_logs:
                    self.logs.extend(scope_logs.log_records)


def _http_collector(received: Received):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/v1/traces":
                request = trace_service_pb2.ExportTraceServiceRequest()
                request.ParseFromString(body)
                received.add_traces(request)
                reply = trace_service_pb2.ExportTraceServiceResponse()
            elif self.path == "/v1/logs":
                request = logs_service_pb2.ExportLogsServiceRequest()
                request.ParseFromString(body)
                received.add_logs(request)
                reply = logs_service_pb2.ExportLogsServiceResponse()
            else:
                self.send_error(404)
                return
            payload = reply.SerializeToString()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:  # keep test output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server.server_address[1], server.shutdown


def _grpc_collector(received: Received):
    grpc = pytest.importorskip("grpc")
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

    class Traces(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):  # noqa: N802 - generated API
            received.add_traces(request)
            return trace_service_pb2.ExportTraceServiceResponse()

    class Logs(logs_service_pb2_grpc.LogsServiceServicer):
        def Export(self, request, context):  # noqa: N802 - generated API
            received.add_logs(request)
            return logs_service_pb2.ExportLogsServiceResponse()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Traces(), server)
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(Logs(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return port, lambda: server.stop(grace=None)


@pytest.mark.parametrize("protocol", ["http/protobuf", "grpc"])
def test_spans_and_logs_reach_an_otlp_collector_with_matching_ids(protocol):
    received = Received()
    start = _http_collector if protocol == "http/protobuf" else _grpc_collector
    port, stop = start(received)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.update(
        {
            "OTEL_TRACES_EXPORTER": "otlp",
            "OTEL_LOGS_EXPORTER": "otlp",
            "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
            "SERVICE_NAME": "e2e-service",
            "SLOW_MIN_MS": "1",
            "SLOW_MAX_MS": "5",
            "LOG_LEVEL": "INFO",
        }
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", APP_SCRIPT],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "503"

    spans = {span.name: span for span in received.spans}
    assert {"process_order", "db.query"} <= set(spans), sorted(spans)
    order = spans["process_order"]
    server = next(s for s in received.spans if s.kind == s.SPAN_KIND_SERVER)
    assert order.trace_id == server.trace_id
    assert order.status.code == order.status.STATUS_CODE_ERROR

    logs = {record.body.string_value: record for record in received.logs}
    failed = logs["order.failed"]
    completed = logs["request.completed"]
    # The warning was logged inside process_order; the access line in the server span.
    assert failed.trace_id == order.trace_id
    assert failed.span_id == order.span_id
    assert completed.trace_id == server.trace_id
    assert completed.span_id == server.span_id
    assert failed.severity_text == "WARN"
    attributes = {kv.key: kv.value for kv in failed.attributes}
    assert attributes["order_id"].string_value == "ORD-E2E"
    assert attributes["request_id"].string_value == "e2e-1"

    service_names = {
        kv.value.string_value
        for resource in received.resources
        for kv in resource.attributes
        if kv.key == "service.name"
    }
    assert service_names == {"e2e-service"}
