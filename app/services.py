"""Business logic - the code worth observing.

Auto-instrumentation gives you a server span per request and a client span per
outbound httpx call. The functions here add the layer that actually explains
*what the service was doing*: a manual ``process_order`` span with domain
attributes, a nested ``db.query`` span for the simulated slow dependency, a
custom business counter, and correlated structured logs.
"""

from __future__ import annotations

import asyncio
import random

import structlog
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.config import get_settings
from app.telemetry.metrics import ORDERS_PROCESSED

log = structlog.get_logger("app.services")
tracer = trace.get_tracer("app.services")


class DependencyError(RuntimeError):
    """Raised when a simulated downstream dependency rejects the request."""


async def _simulate_db_query(order_id: str, settings) -> float:
    """Pretend to hit a database. Opens a nested CLIENT span with db.* attributes."""

    with tracer.start_as_current_span("db.query", kind=SpanKind.CLIENT) as span:
        delay = random.uniform(settings.slow_min_ms, settings.slow_max_ms) / 1000.0
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.statement", "SELECT * FROM orders WHERE id = $1")
        span.set_attribute("db.simulated_latency_ms", round(delay * 1000, 2))
        await asyncio.sleep(delay)
        return delay


async def process_order(
    order_id: str,
    *,
    force_outcome: str | None = None,
    settings=None,
) -> dict:
    """Process a single order inside a manually-created span.

    ``force_outcome`` overrides the random failure logic and is used by tests:
    ``"success"`` always succeeds, ``"failure"`` always raises. Left as ``None``
    it fails with probability ``settings.failure_rate``.
    """

    settings = settings or get_settings()

    with tracer.start_as_current_span("process_order") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("app.operation", "process_order")

        latency = await _simulate_db_query(order_id, settings)
        db_latency_ms = round(latency * 1000, 2)

        if force_outcome == "failure":
            failed = True
        elif force_outcome == "success":
            failed = False
        else:
            failed = random.random() < settings.failure_rate

        if failed:
            error = DependencyError(f"inventory service rejected order {order_id}")
            span.set_status(Status(StatusCode.ERROR, "inventory reservation failed"))
            span.record_exception(error)
            ORDERS_PROCESSED.labels(status="failed").inc()
            log.warning("order.failed", order_id=order_id, db_latency_ms=db_latency_ms)
            raise error

        total = round(random.uniform(10.0, 500.0), 2)
        span.set_attribute("order.total_usd", total)
        span.set_status(Status(StatusCode.OK))
        ORDERS_PROCESSED.labels(status="success").inc()
        log.info(
            "order.processed",
            order_id=order_id,
            total_usd=total,
            db_latency_ms=db_latency_ms,
        )
        return {
            "order_id": order_id,
            "status": "confirmed",
            "total_usd": total,
            "db_latency_ms": db_latency_ms,
        }


async def send_confirmation(order_id: str, parent_context=None) -> None:
    """Background task that 'sends' an order confirmation.

    Background tasks run after the response is returned, so the request's span
    has already closed. We capture the request context and re-attach it here so
    the confirmation span joins the same trace instead of starting a new one -
    the pattern you would use for a real worker or queue consumer.
    """

    token = otel_context.attach(parent_context) if parent_context is not None else None
    try:
        with tracer.start_as_current_span("send_confirmation", kind=SpanKind.PRODUCER) as span:
            span.set_attribute("order.id", order_id)
            span.set_attribute("messaging.system", "email")
            span.set_attribute("messaging.destination.name", "order-confirmations")
            await asyncio.sleep(random.uniform(0.05, 0.2))
            log.info("confirmation.sent", order_id=order_id)
    finally:
        if token is not None:
            otel_context.detach(token)


async def simulate_slow_endpoint(settings=None) -> float:
    """Sleep for a random, sometimes-long interval to give the histograms shape."""

    settings = settings or get_settings()
    with tracer.start_as_current_span("slow_dependency", kind=SpanKind.CLIENT) as span:
        delay = random.uniform(settings.slow_min_ms, settings.slow_max_ms * 2) / 1000.0
        span.set_attribute("dependency.name", "report-generator")
        span.set_attribute("dependency.simulated_latency_ms", round(delay * 1000, 2))
        await asyncio.sleep(delay)
        return delay
