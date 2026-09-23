"""Prometheus metrics, the ASGI middleware that records them, and ``/metrics``.

Exposes the RED signals for every route:

* ``http_requests_total``            - Rate and Errors, split by status code.
* ``http_request_duration_seconds``  - Duration, as a latency histogram.
* ``http_requests_in_progress``      - A saturation-style in-flight gauge.

Every label value is bounded:

* ``path`` is the *route template* (``/api/orders/{order_id}``), never the raw
  URL; unmatched paths collapse to ``"unmatched"``.
* ``method`` is one of the standard HTTP methods; anything else a client sends
  (``PROPFIND``, ``X0000`` ...) is recorded as ``_OTHER``, the OpenTelemetry
  semantic-convention value, so junk methods cannot mint new series.
* ``status_code`` is the numeric response status.

When a request runs inside a *sampled* trace, the latency observation carries a
trace-id exemplar, which lets you jump from a spike on the latency panel to the
exact trace that caused it. Unsampled requests get no exemplar, because their
trace was never exported and the link would lead nowhere.
"""

from __future__ import annotations

import gzip
import time

from opentelemetry import trace
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)
from prometheus_client.exposition import choose_encoder, gzip_accepted
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

__all__ = [
    "CONTENT_TYPE_LATEST",
    "IN_PROGRESS",
    "ORDERS_PROCESSED",
    "OTHER_METHOD",
    "REQUESTS",
    "REQUEST_DURATION",
    "STANDARD_METHODS",
    "PrometheusMiddleware",
    "install_metrics_route",
    "make_metrics_endpoint",
    "normalize_method",
]

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

#: The request methods recorded as-is (RFC 9110 plus PATCH from RFC 5789).
STANDARD_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"}
)
#: The label value for any other method, as in the OpenTelemetry HTTP semconv.
OTHER_METHOD = "_OTHER"

DEFAULT_METRICS_PATH = "/metrics"


def normalize_method(method: str | None) -> str:
    """Map a raw request method onto a bounded label value."""

    return method if method in STANDARD_METHODS else OTHER_METHOD


def _resolve_template(scope) -> str:
    """Return the matched route template for a request scope.

    Starlette does not stash the matched route on the scope before the router
    runs, so we match the request against the app's routes ourselves. A route
    that matches the path but not the method (a 405) still reports its
    template. Unmatched paths collapse to a single ``"unmatched"`` label to keep
    404 scans from exploding cardinality.
    """

    app = scope.get("app")
    if app is None or not hasattr(app, "routes"):
        return "unmatched"

    partial: str | None = None
    for route in app.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, "path", "unmatched")
        if match == Match.PARTIAL and partial is None:
            partial = getattr(route, "path", None)
    return partial or "unmatched"


def _current_exemplar() -> dict[str, str] | None:
    """Return a ``{"trace_id": ...}`` exemplar if a *sampled* span is active.

    An unsampled span still has a valid trace id, but nothing about it is ever
    exported, so an exemplar pointing at it would be a dead link in Grafana.
    """

    ctx = trace.get_current_span().get_span_context()
    if ctx is not None and ctx.is_valid and ctx.trace_flags.sampled:
        return {"trace_id": format(ctx.trace_id, "032x")}
    return None


class PrometheusMiddleware:
    """Pure ASGI middleware that records the RED metrics for every request.

    Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so it runs in the same
    task as the endpoint and preserves the OpenTelemetry context - that is what
    lets us read the active trace id for exemplars.
    """

    def __init__(self, app, excluded_paths: tuple[str, ...] | None = None) -> None:
        self.app = app
        if excluded_paths is None:
            excluded_paths = (DEFAULT_METRICS_PATH,)
        # Never measure the scrape endpoint itself, with or without a slash.
        self.excluded_paths = frozenset(
            variant
            for path in excluded_paths
            for variant in (path.rstrip("/") or "/", path.rstrip("/") + "/")
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        method = normalize_method(scope.get("method"))
        template = _resolve_template(scope)

        in_progress = IN_PROGRESS.labels(method=method, path=template)
        in_progress.inc()
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
            in_progress.dec()
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


def make_metrics_endpoint(
    registry: CollectorRegistry = REGISTRY,
    *,
    disable_compression: bool = False,
):
    """Return a Starlette endpoint that serves the Prometheus exposition.

    It keeps everything ``prometheus_client``'s own ASGI app does - ``Accept``
    negotiation between the text format and OpenMetrics (the only format that
    carries exemplars), gzip, and ``?name[]=`` filtering - but is served as a
    plain route, so ``GET /metrics`` answers ``200`` instead of the ``307``
    redirect a ``Mount`` produces. Rendering runs in the threadpool so a large
    registry never blocks the event loop.
    """

    async def metrics(request: Request) -> Response:
        accept = ",".join(request.headers.getlist("accept"))
        accept_encoding = ",".join(request.headers.getlist("accept-encoding"))
        names = request.query_params.getlist("name[]")

        def render() -> tuple[bytes, str]:
            encoder, content_type = choose_encoder(accept)
            target = registry.restricted_registry(names) if names else registry
            return encoder(target), content_type

        body, content_type = await run_in_threadpool(render)
        headers = {"Content-Type": content_type, "Vary": "Accept, Accept-Encoding"}
        if not disable_compression and gzip_accepted(accept_encoding):
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        return Response(content=body, headers=headers)

    return metrics


def install_metrics_route(
    app,
    path: str = DEFAULT_METRICS_PATH,
    registry: CollectorRegistry = REGISTRY,
) -> None:
    """Serve the Prometheus exposition at ``path`` (and ``path/``) on ``app``.

    Both spellings are registered as real routes so neither ever redirects:
    scrapers, ``curl`` without ``-L`` and health probes all get the payload.
    """

    endpoint = make_metrics_endpoint(registry)
    base = path.rstrip("/") or "/"
    for variant in dict.fromkeys((base, base.rstrip("/") + "/")):
        app.add_route(variant, endpoint, methods=["GET"], include_in_schema=False)
