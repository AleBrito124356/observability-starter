"""OpenTelemetry tracing setup.

This wires three things together:

1. A ``TracerProvider`` with a service ``Resource`` and a head-based sampler.
2. An OTLP/gRPC span exporter pointing at the collector (unless disabled).
3. Auto-instrumentation for FastAPI, httpx and the standard ``logging`` module.

The manual spans that give traces their business meaning live in
``app/services.py`` - auto-instrumentation gives you the server and client
spans for free, but the interesting attributes come from spans you open
yourself around the work that matters.
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

_logger = logging.getLogger(__name__)


def build_resource(settings) -> Resource:
    """Build the OpenTelemetry Resource that tags every span with service identity."""

    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        }
    )


def _instrument_libraries(provider: TracerProvider) -> None:
    """Turn on auto-instrumentation for httpx and the logging module.

    Calling ``instrument()`` twice is a no-op (the instrumentors are singletons
    and log a warning), so this is safe to run more than once.
    """

    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    # set_logging_format=False: we render logs with structlog, not the stdlib
    # formatter. LoggingInstrumentor still attaches trace ids to LogRecords for
    # any library that logs through stdlib directly.
    LoggingInstrumentor().instrument(set_logging_format=False)


def configure_tracing(
    app,
    settings,
    *,
    span_processors: list[SpanProcessor] | None = None,
) -> TracerProvider:
    """Configure tracing and instrument the FastAPI ``app``.

    If a real ``TracerProvider`` is already installed globally (for example a
    test harness that injected an in-memory exporter), it is reused as-is and
    no OTLP exporter is added. Otherwise a fresh provider is built from
    ``settings`` and installed.
    """

    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        provider = existing
    else:
        sampler = ParentBased(root=TraceIdRatioBased(settings.trace_sample_ratio))
        provider = TracerProvider(resource=build_resource(settings), sampler=sampler)

        if span_processors:
            for processor in span_processors:
                provider.add_span_processor(processor)
        elif settings.otel_traces_exporter.lower() != "none":
            exporter = OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                insecure=settings.otel_exporter_otlp_insecure,
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _logger.info(
                "tracing exporting via OTLP to %s",
                settings.otel_exporter_otlp_endpoint,
            )

        trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        # Keep health checks and the metrics scrape out of the trace stream.
        excluded_urls="health,metrics",
    )
    _instrument_libraries(provider)

    app.state.tracer_provider = provider
    return provider
