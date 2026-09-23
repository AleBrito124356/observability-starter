"""Metrics registration and exposition."""

from __future__ import annotations

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

from app import business_metrics
from app.telemetry import metrics as m


def test_metric_objects_have_expected_types():
    assert isinstance(m.REQUESTS, Counter)
    assert isinstance(m.REQUEST_DURATION, Histogram)
    assert isinstance(m.IN_PROGRESS, Gauge)
    assert isinstance(business_metrics.ORDERS_PROCESSED, Counter)


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


# --- Regression tests: /metrics endpoint ------------------------------------


def test_metrics_path_answers_200_without_a_redirect(client):
    # A Mount at /metrics used to answer 307 -> /metrics/ with an empty body,
    # which broke `curl /metrics | grep` and scrapers that do not follow
    # redirects. TestClient follows redirects by default, which hid the bug.
    for path in ("/metrics", "/metrics/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/plain")
        assert "http_requests_total" in response.text


def test_metrics_endpoint_negotiates_openmetrics(client):
    response = client.get(
        "/metrics",
        headers={"Accept": "application/openmetrics-text; version=1.0.0"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/openmetrics-text")
    assert response.text.rstrip().endswith("# EOF")


def test_metrics_endpoint_supports_gzip_and_name_filter(client):
    client.get("/api/orders/ORD-1?outcome=ok")
    response = client.get(
        "/metrics?name[]=http_requests_total",
        headers={"Accept-Encoding": "gzip"},
    )
    assert response.headers.get("content-encoding") == "gzip"
    body = response.text  # httpx transparently decompresses
    assert "http_requests_total" in body
    assert "http_request_duration_seconds_bucket" not in body


def test_metrics_endpoint_is_not_self_measured(client):
    client.get("/metrics")
    client.get("/metrics/")
    body = client.get("/metrics").text
    assert 'path="/metrics' not in body


# --- Regression tests: exemplars only for sampled traces ----------------------

_OPENMETRICS = {"Accept": "application/openmetrics-text; version=1.0.0"}


def _traceparent(trace_id: str, sampled: bool) -> str:
    return f"00-{trace_id}-{'b7ad6b7169203331'}-{'01' if sampled else '00'}"


def test_sampled_request_attaches_trace_id_exemplar(client):
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    assert client.get("/", headers={"traceparent": _traceparent(trace_id, True)}).status_code == 200

    body = client.get("/metrics", headers=_OPENMETRICS).text
    assert f'# {{trace_id="{trace_id}"}}' in body


def test_unsampled_request_gets_no_exemplar(client):
    # The parent says "not sampled", so the ParentBased sampler drops the whole
    # trace. An exemplar pointing at it would be a dead link in Grafana.
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    response = client.get("/", headers={"traceparent": _traceparent(trace_id, False)})
    assert response.status_code == 200

    body = client.get("/metrics", headers=_OPENMETRICS).text
    assert "http_request_duration_seconds_bucket" in body
    assert trace_id not in body


# --- Regression tests: bounded method label ----------------------------------


def test_unknown_http_methods_collapse_to_other(client):
    for i in range(5):
        client.request(f"X{i:04d}", "/api/orders/ORD-1")

    body = client.get("/metrics").text
    assert 'method="X0000"' not in body
    assert (
        'http_requests_total{method="_OTHER",path="/api/orders/{order_id}",status_code="405"}'
        in body
    )


def test_normalize_method_keeps_standard_methods():
    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE"):
        assert m.normalize_method(method) == method
    for junk in ("get", "PROPFIND", "", None, "A" * 500):
        assert m.normalize_method(junk) == "_OTHER"
