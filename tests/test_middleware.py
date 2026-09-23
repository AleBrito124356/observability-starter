"""PrometheusMiddleware edge cases: labels, status codes and the in-flight gauge."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.telemetry import setup_telemetry
from app.telemetry.metrics import PrometheusMiddleware
from tests.conftest import make_settings


def _count(path: str, status: str, method: str = "GET") -> float:
    labels = {"method": method, "path": path, "status_code": status}
    return REGISTRY.get_sample_value("http_requests_total", labels) or 0.0


def _in_progress(path: str, method: str = "GET") -> float:
    labels = {"method": method, "path": path}
    return REGISTRY.get_sample_value("http_requests_in_progress", labels) or 0.0


@pytest.fixture()
def edge_app():
    app = FastAPI()

    @app.get("/edge/ok/{item}")
    async def ok(item: str) -> dict:
        return {"item": item}

    @app.get("/edge/handled")
    async def handled() -> dict:
        raise HTTPException(status_code=503, detail="nope")

    @app.get("/edge/boom")
    async def boom() -> dict:
        raise RuntimeError("unhandled")

    setup_telemetry(app, make_settings(), logging=False)
    return TestClient(app, raise_server_exceptions=False)


def test_unmatched_paths_collapse_to_one_label(edge_app):
    before = _count("unmatched", "404")
    for i in range(3):
        assert edge_app.get(f"/scanner/probe-{i}.php").status_code == 404
    assert _count("unmatched", "404") == before + 3
    body = edge_app.get("/metrics").text
    assert "probe-0" not in body


def test_method_not_allowed_keeps_the_route_template(edge_app):
    before = _count("/edge/ok/{item}", "405", method="DELETE")
    assert edge_app.delete("/edge/ok/x").status_code == 405
    assert _count("/edge/ok/{item}", "405", method="DELETE") == before + 1


@pytest.mark.parametrize(
    ("path", "template", "status"),
    [
        ("/edge/ok/a", "/edge/ok/{item}", "200"),
        ("/edge/handled", "/edge/handled", "503"),
        ("/edge/boom", "/edge/boom", "500"),
    ],
)
def test_every_outcome_is_counted_and_the_gauge_returns_to_zero(edge_app, path, template, status):
    before = _count(template, status)
    response = edge_app.get(path)
    assert response.status_code == int(status)
    # An unhandled exception never sends a response start; it still counts as a 500.
    assert _count(template, status) == before + 1
    assert _in_progress(template) == 0.0


def test_duration_is_observed_for_errors_too(edge_app):
    labels = {"method": "GET", "path": "/edge/boom", "status_code": "500"}
    before = REGISTRY.get_sample_value("http_request_duration_seconds_count", labels) or 0.0
    edge_app.get("/edge/boom")
    after = REGISTRY.get_sample_value("http_request_duration_seconds_count", labels)
    assert after == before + 1


def test_gauge_counts_requests_while_they_are_in_flight():
    app = FastAPI()
    release = asyncio.Event()
    seen: list[float] = []

    @app.get("/edge/wait")
    async def wait() -> dict:
        seen.append(_in_progress("/edge/wait"))
        await release.wait()
        return {}

    setup_telemetry(app, make_settings(), logging=False, tracing=False)

    async def scenario() -> None:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            tasks = [asyncio.create_task(client.get("/edge/wait")) for _ in range(3)]
            while len(seen) < 3:
                await asyncio.sleep(0.01)
            assert _in_progress("/edge/wait") == 3.0
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert _in_progress("/edge/wait") == 0.0


def test_non_http_scopes_pass_straight_through():
    calls: list[str] = []

    async def inner(scope, receive, send) -> None:
        calls.append(scope["type"])

    middleware = PrometheusMiddleware(inner)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert calls == ["lifespan"]


def test_custom_metrics_path_is_served_and_excluded():
    app = FastAPI()
    setup_telemetry(app, make_settings(), logging=False, tracing=False, metrics_path="/internal/m")
    client = TestClient(app)
    assert client.get("/internal/m", follow_redirects=False).status_code == 200
    assert client.get("/metrics").status_code == 404
    assert 'path="/internal/m"' not in client.get("/internal/m").text
