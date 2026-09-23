"""OpenTelemetry tracing setup.

This wires three things together:

1. A ``TracerProvider`` with a service ``Resource`` and a head-based sampler.
2. The span exporters selected by ``OTEL_TRACES_EXPORTER`` (``otlp``,
   ``console``, ``none``, or a comma-separated list), with OTLP speaking either
   gRPC or HTTP/protobuf according to ``OTEL_EXPORTER_OTLP_PROTOCOL``.
3. Auto-instrumentation for FastAPI and httpx.

The manual spans that give traces their business meaning live in
``app/services.py`` - auto-instrumentation gives you the server and client
spans for free, but the interesting attributes come from spans you open
yourself around the work that matters.
"""

from __future__ import annotations

import inspect
import logging

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
    SpanProcessor,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from .exporters import parse_exporter_list
from .resource import build_resource

__all__ = [
    "build_otlp_span_exporter",
    "build_resource",
    "build_sampler",
    "build_span_exporters",
    "configure_tracing",
]

_logger = logging.getLogger(__name__)


def build_sampler(settings) -> ParentBased:
    """Head sampling: keep ``TRACE_SAMPLE_RATIO`` of new traces, follow the parent.

    ``ParentBased`` keeps or drops a whole trace as a unit: a request that
    arrives with a sampled ``traceparent`` is always recorded, an unsampled one
    never is, and only root spans roll the dice.
    """

    return ParentBased(root=TraceIdRatioBased(settings.trace_sample_ratio))


def _otlp_endpoint(settings) -> str:
    endpoint = getattr(settings, "otlp_endpoint", None)
    if endpoint:
        return endpoint
    return settings.otel_exporter_otlp_endpoint


def build_otlp_span_exporter(settings) -> SpanExporter:
    """Return an OTLP span exporter for ``OTEL_EXPORTER_OTLP_PROTOCOL``.

    ``grpc`` talks to the collector's 4317 port; ``http/protobuf`` POSTs to
    ``<endpoint>/v1/traces`` (the collector's 4318 port), exactly as the
    OpenTelemetry spec describes for a signal-agnostic base endpoint. The
    exporter modules are imported lazily so ``none``/``console`` never load
    gRPC.
    """

    protocol = getattr(settings, "otel_exporter_otlp_protocol", "grpc")
    endpoint = _otlp_endpoint(settings)
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HTTPSpanExporter,
        )

        return HTTPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    if protocol == "grpc":
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GRPCSpanExporter,
        )

        return GRPCSpanExporter(endpoint=endpoint, insecure=settings.otel_exporter_otlp_insecure)
    raise ValueError(
        f"OTEL_EXPORTER_OTLP_PROTOCOL={protocol!r}: supported values are grpc, http/protobuf"
    )


def build_span_exporters(settings) -> list[SpanExporter]:
    """Build every exporter named in ``OTEL_TRACES_EXPORTER``.

    Unknown names raise ``ValueError`` (fail fast at startup) instead of
    silently falling back to OTLP.
    """

    exporters: list[SpanExporter] = []
    for name in parse_exporter_list(settings.otel_traces_exporter, variable="OTEL_TRACES_EXPORTER"):
        if name == "otlp":
            exporters.append(build_otlp_span_exporter(settings))
        elif name == "console":
            exporters.append(ConsoleSpanExporter())
    return exporters


def _instrument_httpx(provider: TracerProvider) -> None:
    """Turn on auto-instrumentation for every httpx client in the process.

    ``instrument()`` is idempotent (the instrumentor is a singleton that logs a
    warning and returns when called twice), so this is safe to run more than
    once.
    """

    instrumentor = HTTPXClientInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(tracer_provider=provider)


def _instrument_fastapi(app, provider: TracerProvider) -> None:
    kwargs = {
        "tracer_provider": provider,
        # Keep health checks and the metrics scrape out of the trace stream.
        "excluded_urls": "health,metrics",
    }
    # The per-message ASGI "http send"/"http receive" spans carry no useful
    # information and triple the span count of every request. Older
    # instrumentation releases do not know the option, so feature-detect it.
    if "exclude_spans" in inspect.signature(FastAPIInstrumentor.instrument_app).parameters:
        kwargs["exclude_spans"] = ["receive", "send"]
    FastAPIInstrumentor.instrument_app(app, **kwargs)


def configure_tracing(
    app,
    settings,
    *,
    span_processors: list[SpanProcessor] | None = None,
) -> TracerProvider:
    """Configure tracing and instrument the FastAPI ``app``.

    If a real ``TracerProvider`` is already installed globally (for example a
    test harness that injected an in-memory exporter, or a second app in the
    same process), it is reused and no exporter is added from ``settings`` -
    but any ``span_processors`` passed here are still attached to it. Otherwise
    a fresh provider is built from ``settings`` and installed globally.

    ``app.state.tracer_provider_owned`` records whether the provider was built
    here, so shutdown only closes providers this app owns.
    """

    existing = trace.get_tracer_provider()
    if isinstance(existing, TracerProvider):
        provider = existing
        owned = False
        for processor in span_processors or ():
            provider.add_span_processor(processor)
    else:
        provider = TracerProvider(
            resource=build_resource(settings), sampler=build_sampler(settings)
        )
        owned = True

        if span_processors:
            for processor in span_processors:
                provider.add_span_processor(processor)
        else:
            for exporter in build_span_exporters(settings):
                provider.add_span_processor(BatchSpanProcessor(exporter))
                _logger.info(
                    "tracing exporter enabled: %s (%s)",
                    type(exporter).__name__,
                    type(exporter).__module__,
                )

        trace.set_tracer_provider(provider)

    _instrument_fastapi(app, provider)
    _instrument_httpx(provider)

    app.state.tracer_provider = provider
    app.state.tracer_provider_owned = owned
    return provider
