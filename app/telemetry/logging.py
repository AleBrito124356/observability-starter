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

Optionally (``OTEL_LOGS_EXPORTER=otlp``) the same records are also shipped over
OTLP by ``OTelLogHandler``: each structlog event becomes an OpenTelemetry log
record whose body is the event name, whose attributes are the structured fields
and whose trace/span ids come from the active span. The collector forwards them
to Loki, which is what lets Grafana jump from a trace to its logs and back.
"""

from __future__ import annotations

import logging
import re
import sys
import time
import traceback
from uuid import uuid4

import structlog
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from starlette.datastructures import Headers, MutableHeaders

from .exporters import parse_exporter_list
from .resource import build_resource

try:
    # Older SDKs: Logger.emit() needs the SDK LogRecord, which carries the
    # resource itself; handing it the API LogRecord drops the record at export.
    from opentelemetry.sdk._logs import LogRecord as _SDKLogRecord
except ImportError:  # newer SDKs take the API LogRecord and add the resource
    _SDKLogRecord = None

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
        processors=[*processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
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
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # uvicorn only writes access lines when "uvicorn.access" has handlers
    # (directly or through propagation). When the app writes its own
    # structured, trace-correlated request.completed line (LOG_REQUESTS=true,
    # the default), silence uvicorn's plain-text duplicate at the source;
    # otherwise route it through the JSON handler like everything else.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = not getattr(settings, "log_requests", True)


# --- OTLP log export ------------------------------------------------------------

#: structlog keys that are record fields in OpenTelemetry, not attributes.
_RESERVED_EVENT_KEYS = frozenset(
    {"event", "level", "timestamp", "trace_id", "span_id", "trace_flags", "exc_info", "stack"}
)

_SEVERITY = (
    (logging.CRITICAL, SeverityNumber.FATAL, "FATAL"),
    (logging.ERROR, SeverityNumber.ERROR, "ERROR"),
    (logging.WARNING, SeverityNumber.WARN, "WARN"),
    (logging.INFO, SeverityNumber.INFO, "INFO"),
    (logging.DEBUG, SeverityNumber.DEBUG, "DEBUG"),
)


def _severity(levelno: int) -> tuple[SeverityNumber, str]:
    for threshold, number, text in _SEVERITY:
        if levelno >= threshold:
            return number, text
    return SeverityNumber.TRACE, "TRACE"


def _attribute_value(value):
    """Coerce a structlog value into an OpenTelemetry attribute value."""

    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and value:
        kinds = {type(item) for item in value}
        if len(kinds) == 1 and kinds <= {str, bool, int, float}:
            return list(value)  # homogeneous sequences are valid attributes
    return str(value)


def _body_and_attributes(record: logging.LogRecord) -> tuple[str, dict]:
    """Translate a stdlib record (structlog-wrapped or not) to body + attributes."""

    attributes: dict = {}
    if isinstance(record.msg, dict):
        # A structlog event, already run through the shared processor chain.
        event = record.msg
        body = str(event.get("event", ""))
        for key, value in event.items():
            if key in _RESERVED_EVENT_KEYS or key.startswith("_") or value is None:
                continue
            if key == "exception":
                attributes["exception.stacktrace"] = str(value)
                continue
            attributes[key] = _attribute_value(value)
    else:
        # A plain stdlib record (uvicorn, httpx, ...): add the bound context.
        body = record.getMessage()
        for key, value in structlog.contextvars.get_contextvars().items():
            if value is not None:
                attributes[key] = _attribute_value(value)
    if record.exc_info and record.exc_info[0] is not None:
        exc_type, exc_value, exc_tb = record.exc_info
        attributes["exception.type"] = exc_type.__name__
        attributes["exception.message"] = str(exc_value)
        attributes.setdefault(
            "exception.stacktrace",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
    return body, attributes


class OTelLogHandler(logging.Handler):
    """A stdlib handler that forwards records to an OpenTelemetry ``LoggerProvider``.

    It runs synchronously in the thread/task that logged, so the active span is
    the one the log call happened in: the exported record carries the same
    ``trace_id``/``span_id`` as the JSON line on stdout. Records from the
    OpenTelemetry SDK itself are skipped, so a failing exporter cannot feed its
    own error logs back into the pipeline.
    """

    def __init__(self, logger_provider, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self.logger_provider = logger_provider

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "opentelemetry" or record.name.startswith("opentelemetry."):
            return False
        return super().filter(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            body, attributes = _body_and_attributes(record)
            severity_number, severity_text = _severity(record.levelno)
            logger = self.logger_provider.get_logger(record.name)
            fields = {
                "timestamp": int(record.created * 1e9),
                "context": otel_context.get_current(),
                "severity_number": severity_number,
                "severity_text": severity_text,
                "body": body,
                "attributes": attributes,
            }
            if _SDKLogRecord is not None:
                logger.emit(_SDKLogRecord(resource=logger.resource, **fields))
            else:
                logger.emit(LogRecord(**fields))
        except Exception:  # noqa: BLE001 - a log handler must never raise
            self.handleError(record)


def build_log_exporters(settings) -> list:
    """Build every exporter named in ``OTEL_LOGS_EXPORTER`` (lazy imports)."""

    exporters = []
    names = parse_exporter_list(
        getattr(settings, "otel_logs_exporter", "none"), variable="OTEL_LOGS_EXPORTER"
    )
    for name in names:
        if name == "console":
            from opentelemetry.sdk._logs import export as log_export

            # ConsoleLogRecordExporter is the newer name of ConsoleLogExporter.
            console = (
                getattr(log_export, "ConsoleLogRecordExporter", None)
                or log_export.ConsoleLogExporter
            )
            exporters.append(console())
        elif name == "otlp":
            exporters.append(_build_otlp_log_exporter(settings))
    return exporters


def _build_otlp_log_exporter(settings):
    protocol = getattr(settings, "otel_exporter_otlp_protocol", "grpc")
    endpoint = getattr(settings, "otlp_endpoint", None) or settings.otel_exporter_otlp_endpoint
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        return OTLPLogExporter(endpoint=f"{endpoint.rstrip('/')}/v1/logs")
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

    return OTLPLogExporter(endpoint=endpoint, insecure=settings.otel_exporter_otlp_insecure)


def configure_log_export(settings, *, log_processors=None) -> LoggerProvider | None:
    """Ship log records over OTLP (or to the console) in addition to stdout.

    Returns the ``LoggerProvider``, or ``None`` when ``OTEL_LOGS_EXPORTER`` is
    ``none`` and no processors were injected. Injected ``log_processors``
    replace the configured exporters (tests pass an in-memory exporter). The
    handler is attached to the root logger; calling this again replaces it.
    """

    if log_processors:
        processors = list(log_processors)
    else:
        processors = [BatchLogRecordProcessor(e) for e in build_log_exporters(settings)]
    if not processors:
        return None

    provider = LoggerProvider(resource=build_resource(settings))
    for processor in processors:
        provider.add_log_record_processor(processor)

    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h, OTelLogHandler)]:
        root.removeHandler(existing)
    root.addHandler(OTelLogHandler(provider))
    return provider


def remove_log_export(provider) -> None:
    """Detach the ``OTelLogHandler`` bound to ``provider`` from the root logger."""

    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, OTelLogHandler)]:
        if handler.logger_provider is provider:
            root.removeHandler(handler)


# --- Request id + access log ----------------------------------------------------


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
