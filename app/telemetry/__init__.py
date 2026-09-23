"""The reusable telemetry package: logs, metrics and traces in one call.

Copy this folder into your own FastAPI service and wire it up with::

    from app.telemetry import setup_telemetry

    app = FastAPI()
    telemetry = setup_telemetry(app, settings)

``setup_telemetry`` applies the pieces in the order that makes correlation
work - it is the one place that encodes that rule:

1. ``configure_logging``      JSON logs with ``trace_id``/``span_id`` on every line,
   plus ``configure_log_export`` when ``OTEL_LOGS_EXPORTER`` asks for OTLP.
2. ``RequestIDMiddleware``    a request id on logs, the span and the response.
3. ``PrometheusMiddleware``   RED metrics with bounded labels and trace exemplars,
   plus ``GET /metrics``.
4. ``configure_tracing``      the tracer provider, exporters and auto-instrumentation.
   Tracing goes last so the OpenTelemetry server span wraps the middleware
   above; that is what lets them read the active trace id.

``settings`` can be any object with the attributes of ``app.config.Settings``.
The package only uses relative imports and never imports the rest of ``app``
(a test enforces both), so the folder can be copied as-is into any project,
under any name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .logging import (
    RequestIDMiddleware,
    configure_log_export,
    configure_logging,
    remove_log_export,
)
from .metrics import (
    DEFAULT_METRICS_PATH,
    PrometheusMiddleware,
    install_metrics_route,
)
from .tracing import configure_tracing

__all__ = ["Telemetry", "setup_telemetry"]


@dataclass
class Telemetry:
    """Handles to what ``setup_telemetry`` configured, for flushing and shutdown."""

    tracer_provider: Any = None
    tracer_provider_owned: bool = False
    logger_provider: Any = None

    def force_flush(self, timeout_millis: int = 5000) -> None:
        """Push any buffered spans and log records to their exporters now."""

        if self.tracer_provider is not None:
            self.tracer_provider.force_flush(timeout_millis)
        if self.logger_provider is not None:
            self.logger_provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        """Flush everything; shut down only the providers this app created.

        A provider that was already installed when the app was built (a test
        harness, or a second app in the same process) is only flushed, so one
        app's shutdown cannot silence another's telemetry.
        """

        if self.tracer_provider is not None:
            if self.tracer_provider_owned:
                self.tracer_provider.shutdown()
            else:
                self.tracer_provider.force_flush()
        if self.logger_provider is not None:
            # Always created by setup_telemetry, so always ours to close.
            remove_log_export(self.logger_provider)
            self.logger_provider.shutdown()
            self.logger_provider = None


def setup_telemetry(
    app,
    settings,
    *,
    logging: bool = True,
    metrics: bool = True,
    tracing: bool = True,
    metrics_path: str = DEFAULT_METRICS_PATH,
    span_processors=None,
    log_processors=None,
    log_stream=None,
) -> Telemetry:
    """Wire logs, metrics and traces onto ``app`` in the correct order.

    Args:
        app: the FastAPI (or Starlette) application, before it serves traffic.
        settings: an ``app.config.Settings`` (or any object with its fields).
        logging: configure structlog + stdlib JSON logging (process-global).
            Turn off when the host application owns logging configuration.
        metrics: add the RED metrics middleware and the scrape route.
        tracing: build/reuse the tracer provider and auto-instrument.
        metrics_path: where to serve the Prometheus exposition.
        span_processors: extra span processors (tests inject an in-memory
            exporter here). When given for a new provider, they replace the
            exporters named in ``OTEL_TRACES_EXPORTER``.
        log_processors: log record processors for OTLP-style export; when
            given they replace the exporters named in ``OTEL_LOGS_EXPORTER``.
            Export happens even with ``logging=False`` (the handler goes on the
            root logger), but then the host decides the root log level.
        log_stream: where JSON log lines go (default ``sys.stdout``).

    Returns:
        A ``Telemetry`` handle, also stored on ``app.state.telemetry``.
    """

    if logging:
        configure_logging(settings, stream=log_stream)
    logger_provider = configure_log_export(settings, log_processors=log_processors)

    app.add_middleware(RequestIDMiddleware, access_log=getattr(settings, "log_requests", True))
    if metrics:
        app.add_middleware(PrometheusMiddleware, excluded_paths=(metrics_path,))
        install_metrics_route(app, metrics_path)

    telemetry = Telemetry(logger_provider=logger_provider)
    if tracing:
        telemetry.tracer_provider = configure_tracing(
            app, settings, span_processors=span_processors
        )
        telemetry.tracer_provider_owned = app.state.tracer_provider_owned

    app.state.telemetry = telemetry
    return telemetry
