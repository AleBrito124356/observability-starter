"""The load generator's request mix and statistics."""

from __future__ import annotations

import asyncio
import random
from collections import Counter

import httpx
import pytest

from load.generate import (
    ENDPOINTS,
    _percentile,
    _weighted_choice,
    main,
    plan_request,
    send_request,
)


def test_percentile_nearest_rank():
    values = [0.5, 0.1, 0.4, 0.2, 0.3]
    assert _percentile(values, 0) == 0.1
    assert _percentile(values, 50) == 0.3
    assert _percentile(values, 100) == 0.5
    assert _percentile([], 99) == 0.0
    assert _percentile([7.0], 95) == 7.0


def test_weighted_choice_follows_the_weights():
    rng = random.Random(1234)
    picks = Counter(_weighted_choice(rng) for _ in range(12_000))
    total_weight = sum(weight for *_, weight in ENDPOINTS)
    for method, path, weight in ENDPOINTS:
        expected = 12_000 * weight / total_weight
        assert abs(picks[(method, path)] - expected) < expected * 0.1, (path, picks)


def test_weighted_choice_only_returns_known_endpoints():
    known = {(method, path) for method, path, _ in ENDPOINTS}
    rng = random.Random(0)
    assert {_weighted_choice(rng) for _ in range(500)} <= known


def test_plan_is_reproducible_with_a_seed():
    first = [plan_request(random.Random(42)) for _ in range(1)]
    rng_a, rng_b = random.Random(42), random.Random(42)
    plan_a = [plan_request(rng_a) for _ in range(50)]
    plan_b = [plan_request(rng_b) for _ in range(50)]
    assert plan_a == plan_b
    assert first[0] == plan_a[0]


def test_planned_requests_are_well_formed():
    rng = random.Random(7)
    for spec in (plan_request(rng) for _ in range(300)):
        assert "{" not in spec.path
        if spec.template == "/api/orders/{order_id}":
            assert spec.path.startswith("/api/orders/ORD-")
            assert spec.params["outcome"] in {"auto", "ok", "fail"}
        if spec.method == "POST":
            assert spec.json["items"][0]["quantity"] >= 1
        else:
            assert spec.json is None


def test_send_request_uses_method_params_and_body():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rng = random.Random(3)
            specs = [plan_request(rng) for _ in range(40)]
            for spec in specs:
                await send_request(client, "http://svc", spec)

    asyncio.run(go())
    posts = [r for r in captured if r.method == "POST"]
    orders = [r for r in captured if r.url.path.startswith("/api/orders/ORD-")]
    assert posts and all(b'"items"' in r.content for r in posts)
    assert orders and all("outcome" in r.url.params for r in orders)
    assert all(str(r.url).startswith("http://svc/") for r in captured)


@pytest.mark.parametrize("argv", [["--duration", "0"], ["--concurrency", "0"]])
def test_cli_rejects_non_positive_values(argv, capsys):
    assert main(argv) == 2
    assert "must be positive" in capsys.readouterr().err
