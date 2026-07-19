"""A tiny asyncio + httpx load generator to make the dashboards light up.

It fans out a weighted mix of requests across the service's endpoints for a
fixed duration, then prints a summary with a status-code breakdown and client
side latency percentiles.

Usage::

    python load/generate.py --base-url http://localhost:8000 --duration 120 --concurrency 20
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import Counter

import httpx

# (method, path template, weight). Higher weight = hit more often.
ENDPOINTS: list[tuple[str, str, int]] = [
    ("GET", "/", 1),
    ("GET", "/api/orders/{order_id}", 5),
    ("POST", "/api/orders", 3),
    ("GET", "/api/slow", 2),
    ("GET", "/api/external", 1),
]


def _weighted_choice() -> tuple[str, str]:
    total = sum(weight for *_, weight in ENDPOINTS)
    roll = random.uniform(0, total)
    upto = 0.0
    for method, path, weight in ENDPOINTS:
        upto += weight
        if roll <= upto:
            return method, path
    return ENDPOINTS[0][0], ENDPOINTS[0][1]


async def _worker(
    client: httpx.AsyncClient,
    base_url: str,
    stop_at: float,
    stats: Counter,
    latencies: list[float],
    lock: asyncio.Lock,
) -> None:
    while time.perf_counter() < stop_at:
        method, path = _weighted_choice()
        url = base_url + path.replace("{order_id}", f"ORD-{random.randint(1000, 9999)}")
        params: dict[str, str] = {}
        if path == "/api/orders/{order_id}":
            params["outcome"] = random.choices(
                ["auto", "ok", "fail"], weights=[3, 5, 2]
            )[0]

        start = time.perf_counter()
        try:
            if method == "POST":
                response = await client.post(
                    url,
                    json={"items": [{"sku": "SKU-1", "quantity": random.randint(1, 4)}]},
                )
            else:
                response = await client.get(url, params=params)
            elapsed = time.perf_counter() - start
            async with lock:
                stats[response.status_code] += 1
                latencies.append(elapsed)
        except httpx.HTTPError:
            async with lock:
                stats["error"] += 1


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
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
        print(f"  {str(code):>7}: {stats[code]}")
    if latencies:
        print("-" * 52)
        print(
            "latency  "
            f"p50={_percentile(latencies, 50) * 1000:.1f}ms  "
            f"p95={_percentile(latencies, 95) * 1000:.1f}ms  "
            f"p99={_percentile(latencies, 99) * 1000:.1f}ms"
        )
    print("=" * 52)


async def run(base_url: str, duration: float, concurrency: int) -> None:
    stats: Counter = Counter()
    latencies: list[float] = []
    lock = asyncio.Lock()
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)

    print(f"load: {concurrency} workers hitting {base_url} for {duration:.0f}s ...")
    async with httpx.AsyncClient(timeout=10.0, limits=limits) as client:
        stop_at = time.perf_counter() + duration
        workers = [
            asyncio.create_task(_worker(client, base_url, stop_at, stats, latencies, lock))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*workers)

    _report(stats, latencies, duration)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load generator for observability-starter.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Service base URL.")
    parser.add_argument("--duration", type=float, default=60.0, help="Seconds to run.")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent workers.")
    args = parser.parse_args()

    asyncio.run(run(args.base_url.rstrip("/"), args.duration, args.concurrency))


if __name__ == "__main__":
    main()
