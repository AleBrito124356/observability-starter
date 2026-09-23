"""A faithful Python port of Prometheus' ``histogram_quantile`` for classic histograms.

The dashboard's latency panel runs::

    histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

The offline demo computes the same p50/p95/p99 from the live registry, so this
module reproduces Prometheus' algorithm (``bucketQuantile`` in
``promql/quantile.go``) rather than computing exact percentiles from raw
samples: the answer is linearly interpolated inside the bucket that holds the
requested rank, exactly as Grafana would show it. That interpolation is also
why a p99 can never be more precise than the bucket layout allows.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

Bucket = tuple[float, float]  # (upper bound "le", cumulative count)


def _coalesce(buckets: list[Bucket]) -> list[Bucket]:
    """Merge buckets that share an upper bound by summing their counts."""

    merged: list[Bucket] = []
    for upper, count in buckets:
        if merged and merged[-1][0] == upper:
            merged[-1] = (upper, merged[-1][1] + count)
        else:
            merged.append((upper, count))
    return merged


def _ensure_monotonic(buckets: list[Bucket]) -> list[Bucket]:
    """Force cumulative counts to be non-decreasing (scrape races can break it)."""

    fixed: list[Bucket] = []
    running = -math.inf
    for upper, count in buckets:
        running = max(running, count)
        fixed.append((upper, running))
    return fixed


def histogram_quantile(q: float, buckets: Iterable[Bucket]) -> float:
    """Return the ``q``-quantile of a classic histogram, the way PromQL does.

    Args:
        q: the quantile, 0 <= q <= 1 (outside that range: -Inf / +Inf).
        buckets: ``(le, cumulative_count)`` pairs in any order, including the
            ``+Inf`` bucket.

    Returns:
        The interpolated quantile, or ``NaN`` when it is undefined (no ``+Inf``
        bucket, fewer than two buckets, or zero observations). When the rank
        falls in the ``+Inf`` bucket the answer is the highest finite bound,
        because nothing is known about how far beyond it the values lie.
    """

    if math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf

    ordered = sorted(((float(le), float(count)) for le, count in buckets), key=lambda b: b[0])
    if not ordered or not math.isinf(ordered[-1][0]):
        return math.nan
    ordered = _ensure_monotonic(_coalesce(ordered))
    if len(ordered) < 2:
        return math.nan

    observations = ordered[-1][1]
    if observations == 0:
        return math.nan

    rank = q * observations
    # First bucket (excluding +Inf) whose cumulative count reaches the rank.
    b = next((i for i in range(len(ordered) - 1) if ordered[i][1] >= rank), len(ordered) - 1)

    if b == len(ordered) - 1:
        return ordered[-2][0]
    if b == 0 and ordered[0][0] <= 0:
        return ordered[0][0]

    bucket_start = 0.0
    bucket_end, count = ordered[b]
    if b > 0:
        bucket_start = ordered[b - 1][0]
        count -= ordered[b - 1][1]
        rank -= ordered[b - 1][1]
    if count == 0:
        # Only reachable for q == 0 on an empty first bucket: Go yields 0/0 = NaN.
        return math.nan
    return bucket_start + (bucket_end - bucket_start) * (rank / count)
