"""Metrics registration and exposition."""

from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

from app.telemetry import metrics as m


def test_metric_objects_have_expected_types():
    assert isinstance(m.REQUESTS, Counter)
    assert isinstance(m.REQUEST_DURATION, Histogram)
    assert isinstance(m.IN_PROGRESS, Gauge)
    assert isinstance(m.ORDERS_PROCESSED, Counter)


def test_core_series_are_registered():
    names = set(REGISTRY._names_to_collectors)
    assert "http_requests_total" in names
    assert "http_request_duration_seconds_bucket" in names
    assert "http_requests_in_progress" in names
    assert "orders_processed_total" in names


def test_metrics_endpoint_exposes_series(client):
    # Generate at least one sample for each family.
    assert client.get("/api/orders/ORD-1?outcome=ok").status_code == 200

    body = client.get("/metrics").text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds_bucket" in body
    assert "http_requests_in_progress" in body
    assert "orders_processed_total" in body


def test_path_label_uses_route_template_not_raw_url(client):
    client.get("/api/orders/ORD-abc-123?outcome=ok")
    body = client.get("/metrics").text
    # The bounded template label must appear...
    assert 'path="/api/orders/{order_id}"' in body
    # ...and the raw id must NOT (that would be a cardinality leak).
    assert "ORD-abc-123" not in body


def test_business_counter_increments_on_success(client):
    before = REGISTRY.get_sample_value(
        "orders_processed_total", {"status": "success"}
    ) or 0.0

    response = client.post("/api/orders", json={"items": [{"sku": "SKU-9", "quantity": 2}]})
    assert response.status_code == 202

    after = REGISTRY.get_sample_value(
        "orders_processed_total", {"status": "success"}
    ) or 0.0
    assert after == before + 1.0
