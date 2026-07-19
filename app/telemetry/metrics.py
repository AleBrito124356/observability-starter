"""Prometheus metrics and the ASGI middleware that records them.

Exposes the RED signals for every route:

* ``http_requests_total``            - Rate and Errors, split by status code.
* ``http_request_duration_seconds``  - Duration, as a latency histogram.
* ``http_requests_in_progress``      - A saturation-style in-flight gauge.
* ``orders_processed_total``         - A custom business counter.

Paths are labelled with the *route template* (``/api/orders/{order_id}``),
never the raw URL, so cardinality stays bounded no matter how many distinct
order ids come through. When a request happens inside a sampled trace, the
latency observation carries a trace-id exemplar, which lets you jump straight
from a spike on the latency panel to the exact trace that caused it.
"""

from __future__ import annotations

import time

from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, make_asgi_app
from starlette.routing import Match

# --- Metric definitions -----------------------------------------------------
# prometheus_client appends "_total" to counter names, so we register them
# without the suffix to avoid "http_requests_total_total".

REQUESTS = Counter(
    "http_requests",
    "Total HTTP requests processed, by method, route template and status code.",
    ["method", "path", "status_code"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, by method, route template and status code.",
    ["method", "path", "status_code"],
    buckets=(
        0.005, 0.01, 0.025, 0.05, 0.075,
        0.1, 0.25, 0.5, 0.75,
        1.0, 2.5, 5.0, 10.0,
    ),
)

IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being served, by method and route template.",
    ["method", "path"],
)

ORDERS_PROCESSED = Counter(
    "orders_processed",
    "Business orders processed, labelled by outcome.",
    ["status"],
)

__all__ = [
    "REQUESTS",
    "REQUEST_DURATION",
    "IN_PROGRESS",
    "ORDERS_PROCESSED",
    "PrometheusMiddleware",
    "metrics_asgi_app",
    "CONTENT_TYPE_LATEST",
]


def _resolve_template(scope) -> str:
    """Return the matched route template for a request scope.

    Starlette does not stash the matched route on the scope, so we match the
    request against the app's routes ourselves. Unmatched paths collapse to a
    single ``"unmatched"`` label to keep 404 scans from exploding cardinality.
    """

    app = scope.get("app")
    raw_path = scope.get("path", "")
    if app is None:
        return raw_path or "unknown"

    partial: str | None = None
    for route in app.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, "path", raw_path)
        if match == Match.PARTIAL and partial is None:
            partial = getattr(route, "path", raw_path)
    return partial or "unmatched"


def _current_exemplar() -> dict[str, str] | None:
    """Return a ``{"trace_id": ...}`` exemplar if a valid span is active."""

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is not None and ctx.is_valid:
        return {"trace_id": format(ctx.trace_id, "032x")}
    return None


class PrometheusMiddleware:
    """Pure ASGI middleware that records the RED metrics for every request.

    Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so it runs in the same
    task as the endpoint and preserves the OpenTelemetry context - that is what
    lets us read the active trace id for exemplars.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/metrics" or path.startswith("/metrics/"):
            # Never measure the scrape endpoint itself.
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        template = _resolve_template(scope)

        IN_PROGRESS.labels(method=method, path=template).inc()
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            code = str(status_code)
            IN_PROGRESS.labels(method=method, path=template).dec()
            REQUESTS.labels(method=method, path=template, status_code=code).inc()

            hist = REQUEST_DURATION.labels(method=method, path=template, status_code=code)
            exemplar = _current_exemplar()
            if exemplar is not None:
                try:
                    hist.observe(elapsed, exemplar=exemplar)
                except (ValueError, TypeError):
                    # Exemplars are best-effort; never fail a request over one.
                    hist.observe(elapsed)
            else:
                hist.observe(elapsed)


def metrics_asgi_app():
    """Return the Prometheus exposition ASGI app to mount at ``/metrics``.

    Using the client's own ASGI app (rather than a hand-rolled route) gives us
    OpenMetrics content negotiation for free, which is what carries exemplars
    to Prometheus.
    """

    return make_asgi_app()
