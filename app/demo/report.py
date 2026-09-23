"""Build and render the metrics -> exemplar -> trace -> logs pivot.

Everything here works on *real* telemetry objects: the samples of the live
Prometheus registry (``prometheus_client``), finished OpenTelemetry spans
(``ReadableSpan``) and the JSON lines the production logging pipeline wrote.
Nothing is simulated at this layer; the demo runner only decides what traffic
to send.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

from app.demo.promql import histogram_quantile

REQUESTS_SAMPLE = "http_requests_total"
BUCKET_SAMPLE = "http_request_duration_seconds_bucket"
QUANTILES = (0.50, 0.95, 0.99)

#: Span attributes worth showing in the waterfall, in display order. Both the
#: old and the stable HTTP semantic-convention names are listed, so the output
#: works whichever one the instrumentation emits.
KEY_ATTRIBUTES = (
    "http.method",
    "http.request.method",
    "http.route",
    "http.status_code",
    "http.response.status_code",
    "http.url",
    "url.full",
    "order.id",
    "order.total_usd",
    "db.system",
    "db.operation",
    "db.simulated_latency_ms",
    "dependency.name",
    "upstream.status_code",
    "messaging.destination.name",
    "request.id",
)

SampleKey = tuple[str, tuple[tuple[str, str], ...]]


# --- Registry sampling ---------------------------------------------------------


@dataclass(frozen=True)
class SampleValue:
    value: float
    exemplar_trace_id: str | None = None
    exemplar_value: float | None = None


def registry_samples(registry) -> dict[SampleKey, SampleValue]:
    """Snapshot the RED samples (and their exemplars) of a Prometheus registry."""

    samples: dict[SampleKey, SampleValue] = {}
    for metric in registry.collect():
        for sample in metric.samples:
            if sample.name not in (REQUESTS_SAMPLE, BUCKET_SAMPLE):
                continue
            key = (sample.name, tuple(sorted(sample.labels.items())))
            exemplar = sample.exemplar
            samples[key] = SampleValue(
                value=float(sample.value),
                exemplar_trace_id=(exemplar.labels.get("trace_id") if exemplar else None),
                exemplar_value=(float(exemplar.value) if exemplar else None),
            )
    return samples


def _delta(
    before: dict[SampleKey, SampleValue], after: dict[SampleKey, SampleValue], key: SampleKey
) -> float:
    previous = before.get(key)
    return after[key].value - (previous.value if previous else 0.0)


def _le(value: str) -> float:
    return math.inf if value in ("+Inf", "Inf", "inf") else float(value)


# --- RED -----------------------------------------------------------------------


@dataclass
class RouteStats:
    path: str
    requests: int
    errors: int
    rate_per_second: float
    p50_seconds: float
    p95_seconds: float
    p99_seconds: float

    @property
    def error_ratio(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


def red_by_route(
    before: dict[SampleKey, SampleValue],
    after: dict[SampleKey, SampleValue],
    elapsed_seconds: float,
) -> list[RouteStats]:
    """Rate, errors and duration quantiles per route template, for this run only.

    Counters are cumulative for the life of the process, so every number is
    the difference between two snapshots - the same thing ``rate()`` or
    ``increase()`` does over a range in PromQL.
    """

    requests: dict[str, float] = defaultdict(float)
    errors: dict[str, float] = defaultdict(float)
    buckets: dict[str, dict[float, float]] = defaultdict(lambda: defaultdict(float))

    for key in after:
        name, labels = key
        label_map = dict(labels)
        path = label_map.get("path", "")
        delta = _delta(before, after, key)
        if name == REQUESTS_SAMPLE:
            requests[path] += delta
            if label_map.get("status_code", "").startswith("5"):
                errors[path] += delta
        elif name == BUCKET_SAMPLE:
            # sum by (le) - collapse method and status_code.
            buckets[path][_le(label_map["le"])] += delta

    stats = []
    for path, count in requests.items():
        if count <= 0:
            continue
        per_le = list(buckets[path].items())
        p50, p95, p99 = (histogram_quantile(q, per_le) for q in QUANTILES)
        stats.append(
            RouteStats(
                path=path,
                requests=int(count),
                errors=int(errors[path]),
                rate_per_second=count / elapsed_seconds if elapsed_seconds > 0 else 0.0,
                p50_seconds=p50,
                p95_seconds=p95,
                p99_seconds=p99,
            )
        )
    stats.sort(key=lambda s: (-s.requests, s.path))
    return stats


def status_code_counts(
    before: dict[SampleKey, SampleValue], after: dict[SampleKey, SampleValue]
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for key in after:
        if key[0] == REQUESTS_SAMPLE:
            counts[dict(key[1])["status_code"]] += int(_delta(before, after, key))
    return {code: n for code, n in sorted(counts.items()) if n > 0}


# --- Exemplar hop ----------------------------------------------------------------


@dataclass
class ExemplarHop:
    trace_id: str
    bucket_le: str
    labels: dict[str, str]
    value_seconds: float


def pick_exemplar(
    before: dict[SampleKey, SampleValue],
    after: dict[SampleKey, SampleValue],
    known_trace_ids: Iterable[str] | None = None,
    series_filter=None,
) -> ExemplarHop | None:
    """Return the exemplar on the slowest latency bucket that filled up this run.

    ``prometheus_client`` keeps one exemplar per *bucket* (the latest
    observation that landed in it), so the exemplar of the highest non-empty
    bucket is a request from the slow tail - the dot you would click at the
    top of the latency panel. Ties go to the larger observed value. Only
    exemplars whose trace we actually captured are considered, and
    ``series_filter(labels)`` can narrow the series (e.g. to 5xx responses).
    """

    known = set(known_trace_ids) if known_trace_ids is not None else None
    series: dict[tuple, list[tuple[float, SampleKey]]] = defaultdict(list)
    for key in after:
        name, labels = key
        if name != BUCKET_SAMPLE:
            continue
        without_le = tuple(item for item in labels if item[0] != "le")
        if series_filter is not None and not series_filter(dict(without_le)):
            continue
        series[without_le].append((_le(dict(labels)["le"]), key))

    best: tuple[float, float, ExemplarHop] | None = None
    for without_le, entries in series.items():
        entries.sort()
        previous_cumulative = 0.0
        for le, key in entries:
            cumulative = _delta(before, after, key)
            in_bucket = cumulative - previous_cumulative
            previous_cumulative = cumulative
            sample = after[key]
            if in_bucket <= 0 or sample.exemplar_trace_id is None:
                continue
            if known is not None and sample.exemplar_trace_id not in known:
                continue
            candidate = (le, sample.exemplar_value or 0.0)
            if best is None or candidate > best[:2]:
                label_map = dict(key[1])
                hop = ExemplarHop(
                    trace_id=sample.exemplar_trace_id,
                    bucket_le=label_map["le"],
                    labels=dict(without_le),
                    value_seconds=sample.exemplar_value or 0.0,
                )
                best = (*candidate, hop)
    return best[2] if best else None


# --- Trace waterfall ---------------------------------------------------------------


@dataclass
class SpanRow:
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    depth: int
    tree: str
    start_offset_ms: float
    duration_ms: float
    status: str
    status_description: str | None
    attributes: dict[str, object] = field(default_factory=dict)
    exceptions: list[str] = field(default_factory=list)


def _fmt_id(value: int, width: int) -> str:
    return format(value, f"0{width}x")


def build_waterfall(spans: Sequence, trace_id: str) -> list[SpanRow]:
    """Arrange the finished spans of one trace as a depth-first tree."""

    members = [s for s in spans if _fmt_id(s.context.trace_id, 32) == trace_id]
    if not members:
        return []
    by_id = {s.context.span_id: s for s in members}
    children: dict[int | None, list] = defaultdict(list)
    for span in members:
        parent = span.parent.span_id if span.parent is not None else None
        children[parent if parent in by_id else None].append(span)
    for siblings in children.values():
        siblings.sort(key=lambda s: (s.start_time, s.name))

    origin = min(s.start_time for s in members)
    rows: list[SpanRow] = []

    def visit(span, depth: int, prefix: str, is_last: bool) -> None:
        connector = "" if depth == 0 else ("`- " if is_last else "|- ")
        attributes = {k: span.attributes[k] for k in KEY_ATTRIBUTES if k in span.attributes}
        exceptions = [
            f"{event.attributes.get('exception.type', 'Exception')}: "
            f"{event.attributes.get('exception.message', '')}"
            for event in span.events
            if event.name == "exception"
        ]
        rows.append(
            SpanRow(
                span_id=_fmt_id(span.context.span_id, 16),
                parent_span_id=_fmt_id(span.parent.span_id, 16) if span.parent else None,
                name=span.name,
                kind=span.kind.name,
                depth=depth,
                tree=prefix + connector,
                start_offset_ms=(span.start_time - origin) / 1e6,
                duration_ms=(span.end_time - span.start_time) / 1e6,
                status=span.status.status_code.name,
                status_description=span.status.description,
                attributes=attributes,
                exceptions=exceptions,
            )
        )
        kids = children.get(span.context.span_id, [])
        child_prefix = prefix + ("" if depth == 0 else ("   " if is_last else "|  "))
        for index, child in enumerate(kids):
            visit(child, depth + 1, child_prefix, index == len(kids) - 1)

    roots = children[None]
    for index, root in enumerate(roots):
        visit(root, 0, "", index == len(roots) - 1)
    return rows


# --- Logs ------------------------------------------------------------------------


def parse_json_lines(text: str) -> list[dict]:
    """Parse one-JSON-object-per-line log output, skipping anything else."""

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def logs_for_trace(entries: Iterable[dict], trace_id: str) -> list[dict]:
    """The log lines of one trace - what Loki's ``| trace_id="..."`` returns."""

    return [entry for entry in entries if entry.get("trace_id") == trace_id]


# --- The report ------------------------------------------------------------------


def is_server_error(labels: dict[str, str]) -> bool:
    return labels.get("status_code", "").startswith("5")


@dataclass
class Pivot:
    """One metrics -> exemplar -> trace -> logs walk."""

    reason: str
    question: str
    exemplar: ExemplarHop | None
    trace: list[SpanRow]
    logs: list[dict]

    @property
    def trace_duration_ms(self) -> float:
        return max((r.start_offset_ms + r.duration_ms for r in self.trace), default=0.0)


@dataclass
class PivotReport:
    config: dict
    elapsed_seconds: float
    sent: int
    status_codes: dict[str, int]
    routes: list[RouteStats]
    pivots: list[Pivot]

    def to_json(self) -> dict:
        """A JSON-safe dict (NaN quantiles become ``null``)."""

        def clean(value):
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                return None
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        routes = []
        for route in self.routes:
            entry = asdict(route)
            entry["error_ratio"] = route.error_ratio
            routes.append(entry)
        pivots = []
        for pivot in self.pivots:
            trace = None
            if pivot.exemplar is not None:
                trace = {
                    "trace_id": pivot.exemplar.trace_id,
                    "duration_ms": pivot.trace_duration_ms,
                    "spans": [asdict(row) for row in pivot.trace],
                }
            pivots.append(
                {
                    "reason": pivot.reason,
                    "question": pivot.question,
                    "exemplar": asdict(pivot.exemplar) if pivot.exemplar else None,
                    "trace": trace,
                    "logs": pivot.logs,
                }
            )
        return clean(
            {
                "config": self.config,
                "elapsed_seconds": self.elapsed_seconds,
                "requests_sent": self.sent,
                "requests_served": sum(self.status_codes.values()),
                "status_codes": self.status_codes,
                "routes": routes,
                "pivots": pivots,
            }
        )


def _ms(seconds: float) -> str:
    if seconds is None or math.isnan(seconds):
        return "-"
    return f"{seconds * 1000:.1f}ms"


def _bar(offset: float, duration: float, total: float, width: int = 30) -> str:
    if total <= 0:
        return "[" + "#" * width + "]"
    start = min(width - 1, int(offset / total * width))
    length = max(1, round(duration / total * width))
    length = min(length, width - start)
    return "[" + " " * start + "#" * length + " " * (width - start - length) + "]"


def _format_attributes(attributes: dict) -> str:
    return " ".join(f"{key}={value}" for key, value in attributes.items())


def _render_pivot(pivot: Pivot, number: str) -> list[str]:
    lines = [
        "",
        f"{number} {pivot.question}",
        "",
        "   EXEMPLAR - the dot you would click on the latency panel",
    ]
    hop = pivot.exemplar
    if hop is None:
        lines.append(f"   no exemplar for this pivot ({pivot.reason}): nothing matched in this run")
        return lines
    series = ",".join(f'{k}="{v}"' for k, v in sorted(hop.labels.items()))
    lines += [
        f'   bucket: http_request_duration_seconds_bucket{{le="{hop.bucket_le}",{series}}}',
        f"   exemplar: observed {hop.value_seconds * 1000:.1f}ms -> trace_id={hop.trace_id}",
        "",
        (
            f"   TRACE - {len(pivot.trace)} spans, {pivot.trace_duration_ms:.1f}ms "
            "(the waterfall Tempo shows)"
        ),
        "",
        f"   {'offset':>9} {'duration':>9}  {'timeline':<32}  span",
    ]
    total_ms = pivot.trace_duration_ms
    for row in pivot.trace:
        mark = "  !! ERROR" + (f" ({row.status_description})" if row.status_description else "")
        lines.append(
            f"   {row.start_offset_ms:>7.1f}ms {row.duration_ms:>7.1f}ms  "
            f"{_bar(row.start_offset_ms, row.duration_ms, total_ms)}  "
            f"{row.tree}{row.name} [{row.kind}]"
            f"{mark if row.status == 'ERROR' else ''}"
        )
        indent = " " * 58 + row.tree.replace("`- ", "   ").replace("|- ", "|  ")
        if row.attributes:
            lines.append(f"{indent}   {_format_attributes(row.attributes)}")
        for exception in row.exceptions:
            lines.append(f"{indent}   exception: {exception}")

    lines += [
        "",
        (
            f"   LOGS - {len(pivot.logs)} JSON line{'s' if len(pivot.logs) != 1 else ''} "
            f"with trace_id={hop.trace_id} (what the trace -> logs link returns)"
        ),
        "",
    ]
    lines += [f"   {json.dumps(entry)}" for entry in pivot.logs]
    if not pivot.logs:
        lines.append("   (no log lines for this trace)")
    return lines


def render_text(report: PivotReport) -> str:
    """Render the pivot report for the terminal."""

    cfg = report.config
    served = sum(report.status_codes.values())
    upstream = served - report.sent
    summary = (
        f"sent {report.sent} requests in {report.elapsed_seconds:.1f}s "
        f"(seed {cfg['seed']}, concurrency {cfg['concurrency']}, "
        f"failure rate {cfg['failure_rate']:.0%})"
    )
    if upstream > 0:
        summary += f"; the app also served {upstream} upstream calls made by /api/external"
    lines = [
        "observability-starter - offline pivot demo (in-process: no network, no Docker)",
        summary,
        "responses: " + ", ".join(f"{code} x{n}" for code, n in report.status_codes.items()),
        "",
        "1) METRICS - RED by route, read from the live Prometheus registry",
        "   (quantiles = histogram_quantile over the latency buckets, like the dashboard)",
        "",
        (
            f"   {'route':<26}{'req':>5}{'req/s':>8}{'5xx':>6}{'err%':>8}"
            f"{'p50':>10}{'p95':>10}{'p99':>10}"
        ),
    ]
    for route in report.routes:
        lines.append(
            f"   {route.path:<26}{route.requests:>5}{route.rate_per_second:>8.1f}"
            f"{route.errors:>6}{route.error_ratio:>8.1%}{_ms(route.p50_seconds):>10}"
            f"{_ms(route.p95_seconds):>10}{_ms(route.p99_seconds):>10}"
        )
    for index, pivot in enumerate(report.pivots, start=2):
        lines += _render_pivot(pivot, f"{index})")
    return "\n".join(lines)
