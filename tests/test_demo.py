"""The offline pivot demo: PromQL port, report building and the end-to-end run."""

from __future__ import annotations

import asyncio
import json
import logging
import math

import httpx
import pytest
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.trace import SpanKind

from app.demo import main as demo_main
from app.demo.promql import histogram_quantile
from app.demo.report import build_waterfall, render_text
from app.demo.runner import DemoOptions, NetworkLikeASGITransport, run_demo
from app.main import create_app
from tests.conftest import make_settings

INF = math.inf

# --- histogram_quantile, checked against hand-computed PromQL results ----------

BUCKETS = [(0.1, 10.0), (0.5, 15.0), (1.0, 20.0), (INF, 20.0)]


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        # rank 10 lands exactly on the first bucket's count -> its upper bound.
        (0.5, 0.1),
        # rank 15 -> bucket (0.1, 0.5], 5 of 5 observations -> 0.5.
        (0.75, 0.5),
        # rank 18 -> bucket (0.5, 1.0], 3 of 5 observations -> 0.5 + 0.5 * 0.6.
        (0.9, 0.8),
        # rank 2 -> first bucket, lower bound 0 -> 0.1 * 2/10.
        (0.1, 0.02),
        (1.0, 1.0),
    ],
)
def test_histogram_quantile_interpolates_like_prometheus(q, expected):
    assert histogram_quantile(q, BUCKETS) == pytest.approx(expected)


def test_rank_in_the_inf_bucket_returns_highest_finite_bound():
    # 5 of 10 observations are above 0.1s; nothing is known beyond it.
    assert histogram_quantile(0.99, [(0.1, 5.0), (INF, 10.0)]) == 0.1


def test_input_order_and_duplicate_bounds_do_not_matter():
    shuffled = [(INF, 20.0), (1.0, 12.0), (0.1, 10.0), (0.5, 15.0), (1.0, 8.0)]
    assert histogram_quantile(0.9, shuffled) == pytest.approx(0.8)


def test_non_monotonic_counts_are_repaired():
    # A torn scrape can make a higher bucket smaller; PromQL forces monotonicity.
    assert histogram_quantile(0.5, [(0.1, 10.0), (0.5, 8.0), (INF, 12.0)]) == pytest.approx(0.06)


@pytest.mark.parametrize(
    "buckets",
    [
        [(0.1, 0.0), (INF, 0.0)],  # no observations
        [(0.1, 5.0), (0.5, 10.0)],  # no +Inf bucket
        [(INF, 5.0)],  # a single bucket
        [],
    ],
)
def test_undefined_quantiles_are_nan(buckets):
    assert math.isnan(histogram_quantile(0.5, buckets))


def test_quantile_edge_values():
    assert histogram_quantile(-0.1, BUCKETS) == -INF
    assert histogram_quantile(1.1, BUCKETS) == INF
    assert math.isnan(histogram_quantile(math.nan, BUCKETS))
    # q=0 on an empty first bucket is 0/0 in Prometheus: NaN.
    assert math.isnan(histogram_quantile(0.0, [(0.1, 0.0), (0.5, 4.0), (INF, 4.0)]))
    # A first bucket with a non-positive bound is returned as-is.
    assert histogram_quantile(0.25, [(-1.0, 2.0), (0.0, 4.0), (INF, 4.0)]) == -1.0


# --- The end-to-end demo run ------------------------------------------------------


@pytest.fixture(scope="module")
def demo_report():
    # Shrink the simulated latencies so the whole run takes well under a second.
    mp = pytest.MonkeyPatch()
    mp.setenv("SLOW_MIN_MS", "1")
    mp.setenv("SLOW_MAX_MS", "25")
    try:
        yield asyncio.run(run_demo(DemoOptions(requests=80, concurrency=8, seed=11)))
    finally:
        mp.undo()


def test_demo_counts_every_request(demo_report):
    report = demo_report.to_json()
    assert report["requests_sent"] == 80
    # /api/external makes one extra in-process upstream call each.
    assert report["requests_served"] >= 80
    assert sum(route["requests"] for route in report["routes"]) == report["requests_served"]
    paths = {route["path"] for route in report["routes"]}
    assert "/api/orders/{order_id}" in paths
    assert "/metrics" not in paths


def test_every_pivot_is_one_consistent_trace(demo_report):
    assert [p.reason for p in demo_report.pivots] == ["latency", "errors"]
    for pivot in demo_report.pivots:
        assert pivot.exemplar is not None, pivot.reason
        trace_id = pivot.exemplar.trace_id
        roots = [row for row in pivot.trace if row.depth == 0]
        assert len(roots) == 1
        assert roots[0].kind == "SERVER"
        # The exemplar, the waterfall and every printed log line agree.
        assert pivot.logs, f"no logs for the {pivot.reason} trace"
        assert {entry["trace_id"] for entry in pivot.logs} == {trace_id}
        assert any(entry["event"] == "request.completed" for entry in pivot.logs)


def test_error_pivot_shows_the_failing_order(demo_report):
    pivot = next(p for p in demo_report.pivots if p.reason == "errors")
    assert pivot.exemplar.labels["status_code"].startswith("5")
    rows = {row.name: row for row in pivot.trace}
    order, db = rows["process_order"], rows["db.query"]
    assert db.parent_span_id == order.span_id
    assert db.depth == order.depth + 1
    assert order.status == "ERROR"
    assert order.status_description == "inventory reservation failed"
    assert len(order.exceptions) == 1, "the exception must be recorded exactly once"
    assert any(entry["event"] == "order.failed" for entry in pivot.logs)


def test_latency_pivot_uses_the_slowest_populated_bucket(demo_report):
    latency = demo_report.pivots[0]
    le = float(latency.exemplar.bucket_le)
    # With a 25ms ceiling on simulated latency, nothing should reach 1s.
    assert le < 1.0
    assert latency.exemplar.value_seconds <= le


def test_json_report_round_trips(demo_report):
    payload = json.loads(json.dumps(demo_report.to_json(), allow_nan=False))
    assert payload["pivots"][0]["trace"]["trace_id"] == payload["pivots"][0]["exemplar"]["trace_id"]
    assert payload["routes"][0]["p99_seconds"] is not None


def test_text_report_has_the_four_hops(demo_report):
    text = render_text(demo_report)
    for marker in ("1) METRICS", "LATENCY PIVOT", "ERROR PIVOT", "EXEMPLAR", "TRACE", "LOGS"):
        assert marker in text
    text.encode("ascii")  # safe on any Windows console code page


def test_demo_restores_global_logging(demo_report):
    root = logging.getLogger()
    handlers = root.handlers[:]
    asyncio.run(run_demo(DemoOptions(requests=5, concurrency=2, seed=1)))
    assert root.handlers == handlers


def test_cli_json_output(capsys, monkeypatch):
    monkeypatch.setenv("SLOW_MIN_MS", "1")
    monkeypatch.setenv("SLOW_MAX_MS", "10")
    assert demo_main(["--requests", "30", "--seed", "2", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"] == {"requests": 30, "concurrency": 10, "seed": 2, "failure_rate": 0.1}


def test_cli_rejects_bad_arguments(capsys):
    assert demo_main(["--requests", "0"]) == 2
    assert demo_main(["--failure-rate", "2"]) == 2


# --- In-process upstream: CLIENT span + propagated SERVER span -------------------


async def test_in_process_upstream_call_is_traced_end_to_end(span_exporter):
    app = create_app(
        settings=make_settings(upstream_url="http://upstream.internal/"), enable_logging=False
    )
    app.state.http_client = httpx.AsyncClient(
        transport=AsyncOpenTelemetryTransport(NetworkLikeASGITransport(app=app))
    )
    async with httpx.AsyncClient(
        transport=NetworkLikeASGITransport(app=app), base_url="http://svc"
    ) as client:
        response = await client.get("/api/external")
    await app.state.http_client.aclose()
    assert response.status_code == 200
    assert response.json()["upstream_status"] == 200

    spans = span_exporter.get_finished_spans()
    external_server = next(s for s in spans if s.name == "GET /api/external")
    trace_id = format(external_server.context.trace_id, "032x")
    rows = build_waterfall(spans, trace_id)
    by_name = {row.name: row for row in rows}
    client_row = next(row for row in rows if row.kind == "CLIENT")
    nested_server = by_name["GET /"]
    # server -> external.aggregate -> GET (client) -> GET / (server, via traceparent)
    assert by_name["external.aggregate"].parent_span_id == by_name["GET /api/external"].span_id
    assert client_row.parent_span_id == by_name["external.aggregate"].span_id
    assert nested_server.parent_span_id == client_row.span_id
    nested = next(s for s in spans if s.name == "GET /")
    # A real SERVER span whose parent arrived over the traceparent header.
    assert nested.kind == SpanKind.SERVER
    assert nested.parent.is_remote
