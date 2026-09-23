"""A tiny asyncio + httpx load generator to make the dashboards light up.

It fans out a weighted mix of requests across the service's endpoints for a
fixed duration, then prints a summary with a status-code breakdown and client
side latency percentiles.

Usage::

    python load/generate.py --base-url http://localhost:8000 --duration 120 --concurrency 20
    observability-starter load --base-url http://localhost:8000 --duration 120

The request mix (``ENDPOINTS`` + ``plan_request``) is shared with the offline
demo (``python -m app.demo``), which drives the same traffic in-process.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

# (method, path template, weight). Higher weight = hit more often.
ENDPOINTS: list[tuple[str, str, int]] = [
    ("GET", "/", 1),
    ("GET", "/api/orders/{order_id}", 5),
    ("POST", "/api/orders", 3),
    ("GET", "/api/slow", 2),
    ("GET", "/api/external", 1),
]

#: How often each forced outcome is requested on GET /api/orders/{order_id}.
ORDER_OUTCOMES: tuple[list[str], list[int]] = (["auto", "ok", "fail"], [3, 5, 2])


@dataclass(frozen=True)
class RequestSpec:
    """One planned request: what to send, independent of the transport."""

    method: str
    template: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    json: dict | None = None


def _weighted_choice(rng: random.Random | None = None) -> tuple[str, str]:
    """Pick ``(method, path template)`` from ``ENDPOINTS`` by weight."""

    rng = rng or random
    total = sum(weight for *_, weight in ENDPOINTS)
    roll = rng.uniform(0, total)
    upto = 0.0
    for method, path, weight in ENDPOINTS:
        upto += weight
        if roll <= upto:
            return method, path
    return ENDPOINTS[0][0], ENDPOINTS[0][1]


def plan_request(rng: random.Random | None = None) -> RequestSpec:
    """Plan one request of the weighted mix (concrete path, params and body)."""

    rng = rng or random
    method, template = _weighted_choice(rng)
    path = template.replace("{order_id}", f"ORD-{rng.randint(1000, 9999)}")
    params: dict[str, str] = {}
    body: dict | None = None
    if template == "/api/orders/{order_id}":
        choices, weights = ORDER_OUTCOMES
        params["outcome"] = rng.choices(choices, weights=weights)[0]
    if method == "POST":
        body = {"items": [{"sku": "SKU-1", "quantity": rng.randint(1, 4)}]}
    return RequestSpec(method=method, template=template, path=path, params=params, json=body)


async def send_request(client: httpx.AsyncClient, base_url: str, spec: RequestSpec) -> httpx.Response:
    """Send a planned request with ``client``."""

    return await client.request(
        spec.method, base_url + spec.path, params=spec.params or None, json=spec.json
    )


async def _worker(
    client: httpx.AsyncClient,
    base_url: str,
    stop_at: float,
    stats: Counter,
    latencies: list[float],
    rng: random.Random,
) -> None:
    while time.perf_counter() < stop_at:
        spec = plan_request(rng)
        start = time.perf_counter()
        try:
            response = await send_request(client, base_url, spec)
        except httpx.HTTPError:
            stats["error"] += 1
            continue
        latencies.append(time.perf_counter() - start)
        stats[response.status_code] += 1


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (0.0 for an empty list)."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((pct / 100.0) * (len(ordered) - 1))
    return ordered[index]


def _report(stats: Counter, latencies: list[float], duration: float) -> None:
    total = sum(stats.values())
    print("=" * 52)
    print(
        f"requests: {total}   duration: {duration:.0f}s   "
        f"throughput: {total / duration:.1f} req/s"
    )
    print("-" * 52)
    for code in sorted(stats, key=str):
        print(f"  {code!s:>7}: {stats[code]}")
    if latencies:
        print("-" * 52)
        print(
            "latency  "
            f"p50={_percentile(latencies, 50) * 1000:.1f}ms  "
            f"p95={_percentile(latencies, 95) * 1000:.1f}ms  "
            f"p99={_percentile(latencies, 99) * 1000:.1f}ms"
        )
    print("=" * 52)


async def run(
    base_url: str, duration: float, concurrency: int, seed: int | None = None
) -> tuple[Counter, list[float]]:
    """Drive the mix against ``base_url`` and print the summary."""

    stats: Counter = Counter()
    latencies: list[float] = []
    rng = random.Random(seed)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)

    print(f"load: {concurrency} workers hitting {base_url} for {duration:.0f}s ...")
    async with httpx.AsyncClient(timeout=10.0, limits=limits) as client:
        stop_at = time.perf_counter() + duration
        workers = [
            asyncio.create_task(_worker(client, base_url, stop_at, stats, latencies, rng))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*workers)

    _report(stats, latencies, duration)
    return stats, latencies


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(
        description="Load generator for observability-starter."
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="Service base URL.")
    parser.add_argument("--duration", type=float, default=60.0, help="Seconds to run.")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent workers.")
    parser.add_argument("--seed", type=int, default=None, help="Seed for a repeatable mix.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.duration <= 0 or args.concurrency <= 0:
        print("--duration and --concurrency must be positive", file=sys.stderr)
        return 2
    asyncio.run(run(args.base_url.rstrip("/"), args.duration, args.concurrency, args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
