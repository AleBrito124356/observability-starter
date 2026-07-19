"""Structured JSON logging with trace correlation, plus a request-id middleware.

Two ideas do the heavy lifting:

* ``add_trace_context`` is a structlog processor that reads the active span and
  injects ``trace_id`` / ``span_id`` into every log line. That is the glue that
  lets you pivot from a log entry to its trace in Grafana and back.
* ``RequestIDMiddleware`` assigns each request an id, binds it to structlog's
  context vars so it appears on every log line for that request, echoes it back
  in the ``X-Request-ID`` response header, and records it on the active span.

Both structlog and the standard library (uvicorn's loggers) are routed through
one ``ProcessorFormatter`` so all output is consistent JSON.
"""

from __future__ import annotations

import logging
import sys
from uuid import uuid4

import structlog
from opentelemetry import trace
from starlette.datastructures import Headers, MutableHeaders

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"


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


def configure_logging(settings) -> None:
    """Configure structlog and route stdlib logging through the same formatter."""

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

    handler = logging.StreamHandler(sys.stdout)
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
    """Pure ASGI middleware that attaches a request id to logs, spans and headers."""

    def __init__(self, app, header_name: str = DEFAULT_REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope)
        request_id = incoming.get(self.header_name) or uuid4().hex

        structlog.contextvars.bind_contextvars(request_id=request_id)

        span = trace.get_current_span()
        span_ctx = span.get_span_context()
        if span_ctx is not None and span_ctx.is_valid:
            span.set_attribute("request.id", request_id)

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append(self.header_name, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
