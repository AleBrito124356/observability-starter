"""Endpoints emit spans, and manual spans carry the right attributes and status."""

from __future__ import annotations

from opentelemetry.trace import SpanKind

from app.services import process_order


def test_endpoint_creates_server_and_manual_spans(client, span_exporter):
    response = client.get("/api/orders/ORD-42?outcome=ok")
    assert response.status_code == 200

    spans = span_exporter.get_finished_spans()
    names = [s.name for s in spans]

    # FastAPI auto-instrumentation produces the SERVER span...
    assert any(s.kind == SpanKind.SERVER for s in spans)
    # ...and our business logic produces the manual spans.
    assert "process_order" in names
    assert "db.query" in names

    order_span = next(s for s in spans if s.name == "process_order")
    assert order_span.attributes.get("order.id") == "ORD-42"
    assert order_span.attributes.get("app.operation") == "process_order"


def test_db_span_is_child_of_process_order(client, span_exporter):
    client.get("/api/orders/ORD-7?outcome=ok")
    spans = {s.name: s for s in span_exporter.get_finished_spans()}

    process_span = spans["process_order"]
    db_span = spans["db.query"]
    assert db_span.parent is not None
    assert db_span.parent.span_id == process_span.context.span_id


def test_manual_span_records_error_on_failure(client, span_exporter):
    response = client.get("/api/orders/ORD-99?outcome=fail")
    assert response.status_code == 503

    order_span = next(
        s for s in span_exporter.get_finished_spans() if s.name == "process_order"
    )
    assert order_span.status.status_code.name == "ERROR"
    assert order_span.events, "expected a recorded exception event"


async def test_process_order_success_directly(span_exporter):
    result = await process_order("ORD-DIRECT", force_outcome="success")
    assert result["status"] == "confirmed"

    names = [s.name for s in span_exporter.get_finished_spans()]
    assert "process_order" in names
    assert "db.query" in names
