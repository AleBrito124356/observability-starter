"""FastAPI application factory.

``create_app`` builds a small real service and hands it to
``app.telemetry.setup_telemetry``, which wires the three pillars in the order
that makes correlation work:

* structured JSON logging + a request-id middleware  (logs)
* the Prometheus middleware and the ``/metrics`` route (metrics)
* OpenTelemetry auto-instrumentation + manual spans   (traces)

The module-level ``app`` that ``uvicorn app.main:app`` serves is created on
first access, so importing this module (tests, the offline demo, the CLI) has no
side effects on global logging or tracing.
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

import httpx
import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.services import (
    DependencyError,
    process_order,
    send_confirmation,
    simulate_slow_endpoint,
)
from app.telemetry import setup_telemetry

log = structlog.get_logger("app.main")
tracer = trace.get_tracer("app.main")

#: Timeout for calls made by the app-scoped upstream client.
UPSTREAM_TIMEOUT_SECONDS = 5.0


class OrderItem(BaseModel):
    sku: str = Field(..., description="Stock keeping unit.")
    quantity: int = Field(default=1, ge=1)


class OrderIn(BaseModel):
    order_id: str | None = Field(default=None, description="Optional client-supplied id.")
    items: list[OrderItem] = Field(default_factory=list)


def create_app(
    *,
    settings: Settings | None = None,
    enable_tracing: bool = True,
    enable_metrics: bool = True,
    enable_logging: bool = True,
    span_processors=None,
    log_processors=None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        settings: configuration; defaults to the cached environment settings.
        enable_tracing / enable_metrics / enable_logging: switch pillars off,
            e.g. so tests keep control of the global logging configuration.
        span_processors: extra span processors (tests and the demo inject an
            in-memory exporter here).
        log_processors: log record processors that replace the exporters named
            in OTEL_LOGS_EXPORTER (tests inject an in-memory exporter here).
        http_client: the client ``/api/external`` uses for upstream calls.
            By default one ``httpx.AsyncClient`` is created in the lifespan and
            shared by every request (connection pooling instead of a new client
            per call). Inject one to route upstream calls elsewhere, e.g. an
            in-process ``httpx.ASGITransport`` in the demo or a
            ``MockTransport`` in tests. Set ``app.state.http_client`` after
            creation for a client that needs the app itself.
    """

    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        owned_client: httpx.AsyncClient | None = None
        if getattr(app.state, "http_client", None) is None:
            owned_client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
            app.state.http_client = owned_client
        log.info(
            "service.startup",
            service=settings.service_name,
            version=settings.service_version,
            environment=settings.environment,
        )
        try:
            yield
        finally:
            if owned_client is not None:
                await owned_client.aclose()
                app.state.http_client = None
            log.info("service.shutdown")
            # Flush buffered spans before the process exits. Providers shared
            # with other apps are only flushed, never shut down.
            app.state.telemetry.shutdown()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        description="A reference FastAPI service instrumented with the three pillars of observability.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.http_client = http_client

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
            raise HTTPException(status_code=503, detail=str(exc)) from exc

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
    async def call_external(request: Request) -> dict:
        client: httpx.AsyncClient | None = request.app.state.http_client
        async with contextlib.AsyncExitStack() as stack:
            if client is None:
                # The lifespan did not run (e.g. a TestClient used without
                # `with`): fall back to a short-lived client for this call.
                client = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
                )
            failure: httpx.HTTPError | None = None
            with tracer.start_as_current_span("external.aggregate") as span:
                span.set_attribute("upstream.url", settings.upstream_url)
                try:
                    response = await client.get(settings.upstream_url)
                except httpx.HTTPError as exc:
                    failure = exc
                    span.set_status(Status(StatusCode.ERROR, "upstream request failed"))
                    span.record_exception(exc)
                    log.warning(
                        "upstream.failed", upstream_url=settings.upstream_url, error=repr(exc)
                    )
                else:
                    span.set_attribute("upstream.status_code", response.status_code)
            # Raise outside the span so the SDK does not record a second
            # (HTTPException) event and overwrite the status message.
            if failure is not None:
                raise HTTPException(status_code=502, detail="upstream request failed") from failure
            return {
                "upstream_url": settings.upstream_url,
                "upstream_status": response.status_code,
            }

    # Telemetry goes on last, after the routes exist, and in one call that
    # applies logging -> request id -> metrics -> tracing in the right order.
    setup_telemetry(
        app,
        settings,
        logging=enable_logging,
        metrics=enable_metrics,
        tracing=enable_tracing,
        span_processors=span_processors,
        log_processors=log_processors,
    )
    return app


def __getattr__(name: str):
    """Create the ASGI entrypoint ``app`` lazily (PEP 562).

    ``uvicorn app.main:app`` and ``from app.main import app`` both work, but a
    plain ``import app.main`` no longer configures global logging and tracing
    as a side effect.
    """

    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
