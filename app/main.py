"""FastAPI application factory.

``create_app`` wires the three telemetry pillars onto a small real service:

* structured JSON logging + a request-id middleware  (logs)
* the Prometheus middleware and the ``/metrics`` route (metrics)
* OpenTelemetry auto-instrumentation + manual spans   (traces)

The middleware are added before tracing is configured so the OpenTelemetry
server span ends up outermost - that way the request-id and metrics middleware
run inside an active span and can read the trace id for correlation and
exemplars.
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

import httpx
import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services import (
    DependencyError,
    process_order,
    send_confirmation,
    simulate_slow_endpoint,
)
from app.telemetry.logging import RequestIDMiddleware, configure_logging
from app.telemetry.metrics import PrometheusMiddleware, install_metrics_route
from app.telemetry.tracing import configure_tracing

log = structlog.get_logger("app.main")
tracer = trace.get_tracer("app.main")


class OrderItem(BaseModel):
    sku: str = Field(..., description="Stock keeping unit.")
    quantity: int = Field(default=1, ge=1)


class OrderIn(BaseModel):
    order_id: str | None = Field(default=None, description="Optional client-supplied id.")
    items: list[OrderItem] = Field(default_factory=list)


def create_app(
    *,
    enable_tracing: bool = True,
    enable_metrics: bool = True,
    enable_logging: bool = True,
    span_processors=None,
) -> FastAPI:
    """Build and return the FastAPI application.

    The ``enable_*`` flags exist so tests can build the app with an injected
    in-memory span exporter and without clobbering global logging config.
    """

    settings = get_settings()

    if enable_logging:
        configure_logging(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(
            "service.startup",
            service=settings.service_name,
            version=settings.service_version,
            environment=settings.environment,
        )
        yield
        provider = getattr(app.state, "tracer_provider", None)
        if provider is not None:
            # Flush batched spans before the process exits. Only shut down a
            # provider this app built; a shared one just gets flushed.
            if getattr(app.state, "tracer_provider_owned", False):
                provider.shutdown()
            else:
                provider.force_flush()
        log.info("service.shutdown")

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        description="A reference FastAPI service instrumented with the three pillars of observability.",
        lifespan=lifespan,
    )

    # Order matters: add app middleware first, configure tracing last so the
    # OpenTelemetry server span wraps everything below it.
    app.add_middleware(RequestIDMiddleware)
    if enable_metrics:
        app.add_middleware(PrometheusMiddleware)
        install_metrics_route(app)

    @app.get("/", tags=["meta"])
    async def root() -> dict:
        return {
            "service": settings.service_name,
            "version": settings.service_version,
            "environment": settings.environment,
            "endpoints": [
                "/health",
                "/metrics",
                "/api/orders/{order_id}",
                "/api/orders",
                "/api/slow",
                "/api/external",
            ],
        }

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/orders/{order_id}", tags=["orders"])
    async def get_order(
        order_id: str,
        outcome: str = Query(
            default="auto",
            pattern="^(auto|ok|fail)$",
            description="Force the result: 'ok', 'fail', or 'auto' for random.",
        ),
    ) -> dict:
        force = {"ok": "success", "fail": "failure"}.get(outcome)
        try:
            return await process_order(order_id, force_outcome=force, settings=settings)
        except DependencyError as exc:
            # A handled 5xx: the manual span already recorded the error status.
            raise HTTPException(status_code=503, detail=str(exc))

    @app.post("/api/orders", status_code=202, tags=["orders"])
    async def create_order(payload: OrderIn, background: BackgroundTasks) -> dict:
        order_id = payload.order_id or f"ORD-{uuid4().hex[:8].upper()}"
        result = await process_order(order_id, force_outcome="success", settings=settings)
        # Hand the current trace context to the background task so its span joins
        # this trace instead of starting a detached one.
        background.add_task(send_confirmation, order_id, otel_context.get_current())
        return {"accepted": True, "item_count": len(payload.items), **result}

    @app.get("/api/slow", tags=["demo"])
    async def slow() -> dict:
        delay = await simulate_slow_endpoint(settings)
        return {"slept_ms": round(delay * 1000, 2)}

    @app.get("/api/external", tags=["demo"])
    async def call_external() -> dict:
        async with httpx.AsyncClient(timeout=5.0) as client:
            with tracer.start_as_current_span("external.aggregate") as span:
                span.set_attribute("upstream.url", settings.upstream_url)
                try:
                    response = await client.get(settings.upstream_url)
                except httpx.HTTPError as exc:
                    span.set_status(Status(StatusCode.ERROR, "upstream request failed"))
                    span.record_exception(exc)
                    raise HTTPException(status_code=502, detail="upstream request failed")
                span.set_attribute("upstream.status_code", response.status_code)
                return {
                    "upstream_url": settings.upstream_url,
                    "upstream_status": response.status_code,
                }

    if enable_tracing:
        configure_tracing(app, settings, span_processors=span_processors)

    return app


# The ASGI entrypoint used by uvicorn: `uvicorn app.main:app`.
app = create_app()
