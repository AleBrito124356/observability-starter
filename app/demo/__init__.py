"""Offline "three-pillar pivot" demo.

Runs the real service in-process, drives a seeded request mix through it, and
walks the path an on-call engineer takes in Grafana - with no Docker, no
collector and no network:

1. **Metrics**  - a RED table per route from the live Prometheus registry,
   with p50/p95/p99 computed by a port of PromQL's ``histogram_quantile``.
2. **Exemplar** - the trace id attached to the slowest populated latency bucket
   (and, for the error pivot, to the slowest 5xx bucket).
3. **Trace**    - that trace as an ASCII waterfall (span tree, offsets,
   durations, kinds, key attributes, errors).
4. **Logs**     - every JSON log line carrying that trace id.

Run it with ``python -m app.demo`` or ``observability-starter demo``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

__all__ = ["build_parser", "main", "run_from_args"]


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(
        prog="python -m app.demo",
        description=(
            "Offline pivot demo: metrics -> exemplar -> trace waterfall -> "
            "correlated logs, in-process, in a few seconds."
        ),
    )
    parser.add_argument("--requests", type=int, default=200, help="Requests to send (default 200).")
    parser.add_argument(
        "--concurrency", type=int, default=10, help="Requests in flight at once (default 10)."
    )
    parser.add_argument("--seed", type=int, default=7, help="Seed for the request mix (default 7).")
    parser.add_argument(
        "--failure-rate",
        type=float,
        default=None,
        help="Fraction of 'auto' order reads that fail (default: FAILURE_RATE or 0.1).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print a machine-readable JSON report instead."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_from_args(build_parser().parse_args(argv))


def run_from_args(args: argparse.Namespace) -> int:
    """Run the demo for parsed arguments (shared by ``python -m`` and the CLI)."""

    if args.requests < 1 or args.concurrency < 1:
        print("--requests and --concurrency must be at least 1", file=sys.stderr)
        return 2
    if args.failure_rate is not None and not 0.0 <= args.failure_rate <= 1.0:
        print("--failure-rate must be between 0 and 1", file=sys.stderr)
        return 2

    # Imported here so `--help` stays instant.
    from app.demo.report import render_text
    from app.demo.runner import DemoOptions, run_demo

    report = asyncio.run(
        run_demo(
            DemoOptions(
                requests=args.requests,
                concurrency=args.concurrency,
                seed=args.seed,
                failure_rate=args.failure_rate,
            )
        )
    )
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(render_text(report))
    return 0 if report.pivots and report.pivots[0].exemplar is not None else 1
