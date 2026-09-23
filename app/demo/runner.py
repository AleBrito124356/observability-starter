"""Drive the real app in-process and collect its telemetry for the pivot report.

No sockets are opened: requests go through ``httpx.ASGITransport`` straight
into the ASGI app, and ``/api/external``'s upstream call is routed back into
the same app the same way (wrapped in the OpenTelemetry httpx transport, so it
still produces a CLIENT span and propagates ``traceparent``). Spans land in an
``InMemorySpanExporter``; logs are the exact JSON lines the production logging
pipeline writes, captured from an in-memory stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import io
import logging
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import structlog
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from app.config import Settings
from app.demo.report import (
    Pivot,
    PivotReport,
    build_waterfall,
    is_server_error,
    logs_for_trace,
    parse_json_lines,
    pick_exemplar,
    red_by_route,
    registry_samples,
    status_code_counts,
)
from app.main import create_app
from app.telemetry.logging import configure_logging
from load.generate import plan_request, send_request

#: Where the demo's driver "sends" requests (never resolved: ASGITransport).
DRIVER_BASE_URL = "http://observability-starter.demo"
#: What /api/external calls in the demo: the service's own root endpoint,
#: served in-process, so the trace shows a CLIENT span and a nested SERVER span.
DEMO_UPSTREAM_URL = "http://upstream.demo.internal/"

_STDLIB_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class NetworkLikeASGITransport(httpx.ASGITransport):
    """An ``ASGITransport`` that serves each request the way a real server would.

    Plain ``ASGITransport`` runs the app inside the *caller's* context, so the
    callee would see the caller's active span (and log context) and the
    OpenTelemetry ASGI middleware would open an INTERNAL child span instead of
    a SERVER span. Running the app in a fresh task with an empty
    ``contextvars.Context`` means the only link between the two sides is the
    ``traceparent`` header - exactly as over a socket.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parent = super().handle_async_request
        return await asyncio.create_task(parent(request), context=contextvars.Context())


@dataclass
class DemoOptions:
    requests: int = 200
    concurrency: int = 10
    seed: int = 7
    failure_rate: float | None = None


def demo_settings(options: DemoOptions) -> Settings:
    """Settings for the demo: export nothing, sample everything, JSON logs."""

    overrides: dict = {
        "otel_traces_exporter": "none",
        "trace_sample_ratio": 1.0,
        "log_json": True,
        "log_level": "INFO",
        "log_requests": True,
        "environment": "demo",
        "upstream_url": DEMO_UPSTREAM_URL,
    }
    if options.failure_rate is not None:
        overrides["failure_rate"] = options.failure_rate
    return Settings(_env_file=None, **overrides)


@contextlib.contextmanager
def captured_logging(settings: Settings) -> Iterator[io.StringIO]:
    """Run the production logging setup into a buffer, restoring globals after."""

    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved_loggers = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
        for name in _STDLIB_LOGGERS
    }
    saved_structlog = structlog.get_config()
    buffer = io.StringIO()
    configure_logging(settings, stream=buffer)
    try:
        yield buffer
    finally:
        root.handlers, level = saved_root
        root.setLevel(level)
        for name, (handlers, propagate) in saved_loggers.items():
            logging.getLogger(name).handlers = handlers
            logging.getLogger(name).propagate = propagate
        structlog.configure(**saved_structlog)
        structlog.contextvars.clear_contextvars()


async def _drive(app, plan, concurrency: int) -> None:
    transport = NetworkLikeASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url=DRIVER_BASE_URL, timeout=30.0
    ) as driver:
        pending = iter(plan)

        async def worker() -> None:
            for spec in pending:
                await send_request(driver, "", spec)

        await asyncio.gather(*(worker() for _ in range(concurrency)))


async def run_demo(options: DemoOptions | None = None) -> PivotReport:
    """Build the app, drive the seeded traffic mix through it, return the pivot."""

    options = options or DemoOptions()
    if options.requests < 1 or options.concurrency < 1:
        raise ValueError("requests and concurrency must be at least 1")

    settings = demo_settings(options)
    rng = random.Random(options.seed)
    # The service code draws latencies and failures from the global RNG.
    random.seed(options.seed)
    plan = [plan_request(rng) for _ in range(options.requests)]

    span_exporter = InMemorySpanExporter()
    span_processor = SimpleSpanProcessor(span_exporter)
    before = registry_samples(REGISTRY)

    with captured_logging(settings) as log_buffer:
        app = create_app(settings=settings, enable_logging=False, span_processors=[span_processor])
        provider = app.state.telemetry.tracer_provider
        upstream = httpx.AsyncClient(
            transport=AsyncOpenTelemetryTransport(
                NetworkLikeASGITransport(app=app, raise_app_exceptions=False),
                tracer_provider=provider,
            ),
            timeout=5.0,
        )
        app.state.http_client = upstream

        started = time.perf_counter()
        try:
            async with app.router.lifespan_context(app):
                await _drive(app, plan, options.concurrency)
        finally:
            await upstream.aclose()
        elapsed = time.perf_counter() - started
        log_text = log_buffer.getvalue()

    spans = span_exporter.get_finished_spans()
    # Detach from a shared provider (e.g. under pytest) so it stops collecting.
    span_processor.shutdown()
    after = registry_samples(REGISTRY)

    trace_ids = {format(span.context.trace_id, "032x") for span in spans}
    log_entries = parse_json_lines(log_text)

    def walk(reason: str, question: str, series_filter=None) -> Pivot:
        hop = pick_exemplar(before, after, known_trace_ids=trace_ids, series_filter=series_filter)
        return Pivot(
            reason=reason,
            question=question,
            exemplar=hop,
            trace=build_waterfall(spans, hop.trace_id) if hop else [],
            logs=logs_for_trace(log_entries, hop.trace_id) if hop else [],
        )

    pivots = [
        walk("latency", "LATENCY PIVOT - why is the p99 high? (slowest populated bucket)"),
        walk(
            "errors",
            "ERROR PIVOT - what is behind the 5xx band? (slowest 5xx exemplar)",
            series_filter=is_server_error,
        ),
    ]

    return PivotReport(
        config={
            "requests": options.requests,
            "concurrency": options.concurrency,
            "seed": options.seed,
            "failure_rate": settings.failure_rate,
        },
        elapsed_seconds=elapsed,
        sent=len(plan),
        status_codes=status_code_counts(before, after),
        routes=red_by_route(before, after, elapsed),
        pivots=pivots,
    )
