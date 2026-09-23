"""Business metrics for the demo service.

Kept outside ``app/telemetry/`` on purpose: the telemetry package is generic
and copyable, while a counter like this one is specific to *your* domain. Put
yours next to the code that increments it.
"""

from __future__ import annotations

from prometheus_client import Counter

# prometheus_client appends "_total": the series is ``orders_processed_total``.
ORDERS_PROCESSED = Counter(
    "orders_processed",
    "Business orders processed, labelled by outcome.",
    ["status"],
)

__all__ = ["ORDERS_PROCESSED"]
