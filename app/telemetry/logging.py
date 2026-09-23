"""Structured JSON logging with trace correlation, plus a request-id middleware.

Two ideas do the heavy lifting:

* ``add_trace_context`` is a structlog processor that reads the active span and
  injects ``trace_id`` / ``span_id`` into every log line. That is the glue that
  lets you pivot from a log entry to its trace in Grafana and back.
* ``RequestIDMiddleware`` assigns each request an id, binds it to structlog's
  context vars so it appears on every log line for that request, echoes it back
  in the ``X-Request-ID`` response header, and records it on the active span.
  A client-supplied id is honoured only when it is a short token
  (``[A-Za-z0-9._:-]{1,128}``); anything else - a 6 KB string, spaces, quotes,
  JSON - is replaced by a fresh id so it cannot bloat or forge log lines and
  is never reflected back in a response header. It also writes one structured
  ``request.completed`` access-log line per request (method, raw path, route
  template, status, duration), emitted *inside* the server span so it carries
  the trace id - every request, even one whose handler logs nothing, has at
  least one log line you can reach from its trace.

Both structlog and the standard library (uvicorn's loggers) are routed through
one ``ProcessorFormatter`` so all output is consistent JSON.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from uuid import uuid4

import structlog
from opentelemetry import trace
from starlette.datastructures import Headers, MutableHeaders

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"

#: Paths left out of the access log by default: probes and scrapes would drown
#: the interesting lines (Prometheus alone scrapes every few seconds).
DEFAULT_ACCESS_LOG_EXCLUDE = ("/health", "/metrics", "/metrics/")

access_log = structlog.get_logger("app.access")

#: What an acceptable incoming request id looks like: UUIDs (with or without
#: dashes), ULIDs, W3C trace ids and most load-balancer ids all fit.
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_request_id() -> str:
    """Generate a request id (32 lowercase hex characters)."""

    return uuid4().hex


def sanitize_request_id(candidate: str | None) -> str:
    """Return ``candidate`` if it is a safe request id, otherwise a fresh one."""

    if candidate and REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return new_request_id()


def add_trace_context(logger, method_name, event_dict):
    """structlog processor: inject the active trace/span ids for correlation."""

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx is not None and ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
        event_dict["trace_flags"] = int(ctx.trace_flags)
    return event_dict


def shared_processors(settings=None):
    """The structlog processor chain shared by app logs and foreign stdlib logs.

    Kept as a function so tests can reuse the exact production chain and append
    their own capturing sink to the end.
    """

    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_trace_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(settings, *, stream=None) -> None:
    """Configure structlog and route stdlib logging through the same formatter.

    ``stream`` defaults to ``sys.stdout``; the offline demo passes an in-memory
    buffer to read back the exact JSON lines production would print.
    """

    processors = shared_processors(settings)

    if settings.log_json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    # Let uvicorn's loggers bubble up to the root handler instead of using their
    # own, so their output is JSON too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


class RequestIDMiddleware:
    """Pure ASGI middleware: request id on logs, span and headers, plus an access log.

    Args:
        app: the next ASGI app.
        header_name: request/response header carrying the id.
        access_log: write one ``request.completed`` line per request.
        access_log_exclude: exact paths that get no access-log line.
    """

    def __init__(
        self,
        app,
        header_name: str = DEFAULT_REQUEST_ID_HEADER,
        *,
        access_log: bool = True,
        access_log_exclude: tuple[str, ...] = DEFAULT_ACCESS_LOG_EXCLUDE,
    ) -> None:
        self.app = app
        self.header_name = header_name
        self.access_log = access_log
        self.access_log_exclude = frozenset(access_log_exclude)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope)
        request_id = sanitize_request_id(incoming.get(self.header_name))

        span = trace.get_current_span()
        span_ctx = span.get_span_context()
        if span_ctx is not None and span_ctx.is_valid:
            span.set_attribute("request.id", request_id)

        status_code = 500
        start = time.perf_counter()

        async def send_wrapper(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append(self.header_name, request_id)
            await send(message)

        # bound_contextvars restores the previous value on exit, so a request
        # served in-process from inside another one (a mounted sub-app, an
        # ASGITransport call) does not wipe the outer request's id.
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                if self.access_log and scope.get("path") not in self.access_log_exclude:
                    self._log_access(scope, status_code, time.perf_counter() - start)

    @staticmethod
    def _log_access(scope, status_code: int, elapsed: float) -> None:
        # The router stores the matched route on the (shared) scope, so after
        # the call we get the template for free, without re-matching.
        route = getattr(scope.get("route"), "path", None)
        emit = access_log.warning if status_code >= 500 else access_log.info
        emit(
            "request.completed",
            method=scope.get("method"),
            path=scope.get("path"),
            route=route,
            status_code=status_code,
            duration_ms=round(elapsed * 1000, 2),
        )
