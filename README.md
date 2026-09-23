# observability-starter

**The three pillars for a Python service, wired and working** — OpenTelemetry tracing, Prometheus metrics, and trace-correlated structured logs on a real FastAPI app, with a docker-compose Grafana stack and provisioned dashboards.

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-instrumented-009688)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-traces-425cc7)
![Prometheus](https://img.shields.io/badge/Prometheus-metrics-e6522c)
![Grafana](https://img.shields.io/badge/Grafana-dashboards-f46800)

Most "observability tutorials" show you one pillar in isolation and stop at `print("hello metrics")`. This is the opposite: a small service that is actually worth watching, with all three pillars turned on, exporting to a full local stack you bring up with one command. Copy the `app/telemetry/` package into your own project and you have logs, metrics and traces on day one.

---

## Why

The point of observability is not "collect telemetry". It is being able to answer *why is this request slow or broken?* without redeploying. That takes three signals that reference each other:

| Pillar | Answers | Cost model |
| --- | --- | --- |
| **Metrics** | *Is something wrong, and how bad?* Cheap aggregates over time — rates, error ratios, latency quantiles. | Constant cost, bounded by label cardinality. |
| **Traces** | *Where in the request did the time go?* One request, broken into spans across functions and services. | Per-request, usually sampled. |
| **Logs** | *What exactly happened at this step?* The detail line for one event, with full context. | Per-event, the most expensive at volume. |

You start at metrics (a dashboard alerts you), pivot to a trace (the latency panel's exemplar links to the exact slow request), then read the logs for that trace (they carry the same `trace_id`). This repo wires that pivot end to end: the latency histogram emits trace-id **exemplars**, and every log line carries `trace_id` + `span_id`.

### RED and USE, in four lines

- **RED** — for *request-driven* services, watch **R**ate, **E**rrors, **D**uration. This is the shape of the provided dashboard.
- **USE** — for *resources* like CPU, memory, disk, connection pools, watch **U**tilization, **S**aturation, **E**rrors.
- RED tells you the service is unhappy; USE tells you which resource to blame. The in-progress-requests gauge here is a saturation signal — the bridge between the two.

---

## Architecture

```mermaid
flowchart LR
    load[Load generator] -->|HTTP| app[FastAPI app]
    app -->|OTLP spans + logs| col[OTel Collector]
    col -->|OTLP gRPC| tempo[Tempo]
    col -->|OTLP/HTTP| loki[Loki]
    tempo -->|remote write: service graph + span metrics| prom[Prometheus]
    prom -->|scrape /metrics| app
    prom -->|scrape self-metrics| col
    app -->|JSON logs| stdout[stdout]
    graf[Grafana] -->|PromQL| prom
    graf -->|TraceQL| tempo
    graf -->|LogQL| loki
```

Metrics use a **pull** model — Prometheus scrapes `/metrics` on the app. Traces and logs use a **push** model — the app ships spans and log records over OTLP to the collector, which batches them and forwards spans to Tempo and logs to Loki's native OTLP endpoint. Logs are *also* written to stdout as one JSON object per line, so `docker compose logs` and any platform log agent keep working. Tempo's metrics generator turns the traces into service-graph and span metrics and remote-writes them to Prometheus, which is what fills the service graph.

Every hop of the pivot is a provisioned link, so it is one click each way:

```mermaid
flowchart LR
    spike[Latency spike on a metrics panel] -->|exemplar trace_id| span[The exact trace in Tempo]
    span -->|Logs for this span: trace_id filter| line[Its log lines in Loki]
    line -->|derived field trace_id| span
```

| Hop | Wired by |
| --- | --- |
| metrics → trace | the latency histogram's `trace_id` exemplar (sampled traces only) + Prometheus `exemplarTraceIdDestinations` → Tempo |
| trace → logs | Tempo `tracesToLogsV2` → Loki query `{service_name="observability-starter"} \| trace_id="<id>"` |
| logs → trace | Loki `derivedFields` on the `trace_id` structured-metadata field → Tempo |
| trace → service graph | Tempo `serviceMap` → Prometheus, fed by Tempo's metrics generator |

> **What was verified, and how.** The app side is covered by tests that run offline: spans and log records are exported over real OTLP (both gRPC and HTTP/protobuf) to a fake collector that decodes them with the official protobuf messages, and the test asserts that the log records carry the same `trace_id`/`span_id` as the spans. The stack files (compose, collector, Loki, Tempo, Prometheus, Grafana provisioning and dashboard) are checked by contract tests for internal consistency. `docker compose up` itself was **not** run while writing this version (no Docker on the machine it was built on); see [Checking the Grafana links](#checking-the-grafana-links) for the manual steps.

---

## See the pivot in ten seconds, offline

No Docker, no collector, no network: the demo builds the real app in-process, drives a seeded request mix through it with `httpx.ASGITransport`, and walks the same path you would click through in Grafana.

```bash
python -m app.demo --requests 200 --seed 7        # or --json for a machine-readable report
```

1. **Metrics** — a RED table per route read from the live Prometheus registry, with p50/p95/p99 computed by a Python port of PromQL's `histogram_quantile` (so the numbers match what the dashboard would plot, bucket interpolation included).
2. **Exemplar** — the `trace_id` attached to the slowest populated latency bucket (the *latency pivot*) and to the slowest 5xx bucket (the *error pivot*).
3. **Trace** — that trace as an ASCII waterfall: span tree, offsets, durations, kinds, key attributes, `!! ERROR` marks and recorded exceptions.
4. **Logs** — every JSON log line carrying that `trace_id`, exactly as the production logging pipeline wrote it.

`/api/external`'s upstream call is served in-process too, in a fresh context, so its trace shows the CLIENT span and a SERVER span whose parent arrived over the `traceparent` header — propagation, not shared memory. Flags: `--requests`, `--concurrency`, `--seed`, `--failure-rate`, `--json`.

## Quickstart

```bash
git clone https://github.com/AleBrito124356/observability-starter
cd observability-starter

cp .env.example .env          # every value has a working default

# Bring up app + collector + prometheus + tempo + grafana
docker compose -f deploy/docker-compose.yml up --build -d

# Drive traffic so the dashboards have something to show
python -m pip install httpx
python load/generate.py --duration 120 --concurrency 20
```

Then open:

| URL | What |
| --- | --- |
| http://localhost:3000 | Grafana — dashboard **"observability-starter - RED overview"** (anonymous admin, no login) |
| http://localhost:9090 | Prometheus — query `http_requests_total` directly |
| http://localhost:8000/docs | The instrumented service's OpenAPI UI |
| http://localhost:8000/metrics | Raw Prometheus exposition |

There are **no secrets** in this project and nothing to sign up for. The `.env` exists only for tuning behaviour.

### Run the app on its own, without Docker

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# No collector running? Build spans without exporting them:
OTEL_TRACES_EXPORTER=none uvicorn app.main:app --reload

# ...or print every finished span to stdout instead:
OTEL_TRACES_EXPORTER=console uvicorn app.main:app --reload
```

`OTEL_TRACES_EXPORTER` accepts `otlp`, `console`, `none` or a comma-separated list, and `OTEL_EXPORTER_OTLP_PROTOCOL` selects `grpc` (port 4317) or `http/protobuf` (port 4318). Settings are validated at startup: `TRACE_SAMPLE_RATIO=1.5`, `FAILURE_RATE=7`, `SLOW_MIN_MS` above `SLOW_MAX_MS` or an unknown exporter name stop the service with a message that names the variable.


### Checking the Grafana links

With the stack up and the load generator running (`python load/generate.py --duration 120`), each hop can be checked by hand:

1. **metrics → trace** — open the dashboard, find the purple exemplar dots on *Latency p50 / p95 / p99* (toggle *Exemplars* in the panel's query options if they are hidden), click one and choose **View trace**: Tempo opens that exact trace.
2. **trace → logs** — in the trace view, open a span and click **Logs for this span**: Explore opens Loki with `{service_name="observability-starter"} | trace_id="<that id>"` and shows the request's `order.processed` / `order.failed` / `request.completed` lines.
3. **logs → trace** — in the *Logs for this service* panel, expand any line: its `trace_id` field carries a **View trace** link back to Tempo.
4. **service graph** — Explore → Tempo → *Service Graph* (or the *Service graph* panel). After a minute of traffic it shows the service, its self-call edge (from `/api/external`) and a virtual database node for `db.query`; in Prometheus, `traces_service_graph_request_total` returns series.
5. **Loki directly** — `curl -sG localhost:3100/loki/api/v1/query_range --data-urlencode 'query={service_name="observability-starter"} | trace_id!=""' | head -c 600`.

---

## Usage

Exercise the endpoints and watch the signals move:

```bash
curl -s localhost:8000/api/orders/ORD-1001?outcome=ok | jq
# {"order_id":"ORD-1001","status":"confirmed","total_usd":142.87,"db_latency_ms":63.4}

curl -si localhost:8000/api/orders/ORD-1002?outcome=fail | head -1
# HTTP/1.1 503 Service Unavailable      <- becomes a 5xx on the error panel

curl -s localhost:8000/metrics | grep -E '^http_request.*order_id'
# http_requests_total{method="GET",path="/api/orders/{order_id}",status_code="200"} 1.0
# http_request_duration_seconds_bucket{le="0.1",method="GET",path="/api/orders/{order_id}",status_code="200"} 0.0
# http_requests_in_progress{method="GET",path="/api/orders/{order_id}"} 0.0

# Exemplars only travel in the OpenMetrics format:
curl -s -H 'Accept: application/openmetrics-text; version=1.0.0' localhost:8000/metrics | grep -m1 'bucket.*trace_id'
# http_request_duration_seconds_bucket{le="0.075",method="GET",path="/api/orders/{order_id}",status_code="200"} 1.0 # {trace_id="0af7651916cd43dd8448eb211c80319c"} 0.061 1790180476.51
```

`/metrics` is a plain route, so it answers `200` directly (no `307` redirect to `/metrics/`), and keeps `prometheus_client`'s content negotiation, gzip and `?name[]=` filtering.

Every label value is bounded, so series count does not grow with traffic:

- `path` is the **route template** `/api/orders/{order_id}`, never the raw URL; unknown paths collapse to `unmatched`.
- `method` is one of the nine standard HTTP methods; anything else (`PROPFIND`, `X0000`, ...) is recorded as `_OTHER`, the OpenTelemetry semantic-convention value.
- `status_code` is the numeric status.

A log line looks like this (one JSON object per line, correlated to its trace):

```json
{"event": "order.processed", "order_id": "ORD-1001", "total_usd": 142.87,
 "db_latency_ms": 63.4, "request_id": "f002c93dbbd341bd8093d0289aa2d487",
 "level": "info", "timestamp": "2026-07-19T14:38:02.682182Z",
 "trace_id": "e0f6d1ff36449ad5577cd0f890d3e7ce", "span_id": "5db5a2b066638f8a"}
```

### The dashboard, panel by panel

| Panel | Query intent | Read it as |
| --- | --- | --- |
| **Request rate by route** | `sum by (path) (rate(http_requests_total[...]))` | The **R** in RED — traffic shape per endpoint. |
| **Requests by status code** | rate split by `status_code`, 5xx in red | The **E** in RED — watch the red band grow. |
| **Error rate** | `5xx / total`, as a percent | A single number to alert on. |
| **Requests in progress** | `sum(http_requests_in_progress)` | Saturation — concurrency in flight right now. |
| **Latency p50 / p95 / p99** | `histogram_quantile(...)` over the bucket rate | The **D** in RED — and the p95 line carries exemplars. |
| **Orders processed by outcome** | `rate(orders_processed_total)` by `status` | The custom **business** metric, not just HTTP. |
| **Request latency heatmap** | bucket rate by `le` | The full latency distribution over time. |

Click a purple exemplar dot on the latency panel to jump straight to that trace in Tempo. In the trace you will see the FastAPI server span, the manual `process_order` span with its `order.id` and `order.total_usd` attributes, and the nested `db.query` span — the whole request, decomposed.

### Run the tests

```bash
pip install -r requirements-dev.txt
pytest
# tests/test_logging.py ...      log lines carry trace_id and span_id
# tests/test_metrics.py .....    series registered, exposed, cardinality bounded
# tests/test_tracing.py ....     endpoints emit server + manual spans, errors recorded
# 12 passed
```

---

## Copy the telemetry into your own app

Everything reusable lives in `app/telemetry/`. It imports nothing from the demo service (a test enforces that), and the demo's business counter lives outside it in `app/business_metrics.py`. Drop the folder into your project and wire it up with one call:

```python
from fastapi import FastAPI
from app.config import Settings          # or any object with the same fields
from app.telemetry import setup_telemetry

app = FastAPI()
# ... your routes ...
telemetry = setup_telemetry(app, Settings())
```

`setup_telemetry` applies the pieces in the only order that makes correlation work — JSON logging, then the request-id middleware, then the RED metrics middleware and `GET /metrics`, then tracing last so the OpenTelemetry server span wraps the middleware and they can read the trace id. It returns a `Telemetry` handle (also on `app.state.telemetry`) whose `shutdown()` flushes buffered spans; call it from your lifespan. Each pillar can be switched off (`logging=False` when your host app owns logging config, `metrics=False`, `tracing=False`), and `span_processors=[...]` injects extra processors, which is how the tests and the demo capture spans in memory.

The individual building blocks are still importable if you prefer to wire them by hand: `configure_logging`, `RequestIDMiddleware`, `PrometheusMiddleware`, `install_metrics_route` and `configure_tracing`.

The middleware are deliberately **pure ASGI**, not `BaseHTTPMiddleware`, so they run in the same task as your endpoints and preserve the OpenTelemetry context — that is what makes the trace id available for exemplars and log correlation. Add manual spans around the work that matters, exactly as `app/services.py` does around `process_order`.

## An honest note on overhead and sampling

- **Metrics** are essentially free — a few atomic increments and a histogram observation per request. The real cost is *cardinality*: keep label values bounded (route templates, not raw paths; a fixed set of methods; status codes, not messages). This repo does that for you, and the tests pin it down.
- **Exemplars** are only attached when the request's trace is *sampled*. At `TRACE_SAMPLE_RATIO=0.1`, 90% of requests are not exported to Tempo, so an exemplar pointing at them would be a dead link; they simply carry none.
- **Tracing** is not free. Span creation, attribute serialization and OTLP export cost CPU and bandwidth that scale with request volume. In development, `TRACE_SAMPLE_RATIO=1.0` keeps every trace. In production, lower it (`0.05`–`0.1` is common) — the `ParentBased` sampler here keeps a whole trace or drops it as a unit, so you never get half-recorded traces. Tail-based sampling in the collector is the next step when you want to always keep errors and slow requests.
- **Logs** are the most expensive signal at scale. JSON structured logs are worth it for the queryability, but keep them at `INFO` in production and lean on traces for per-step detail rather than logging inside every function.
- The middleware resolves the route template by matching against the app's routes on each request — an `O(routes)` cost that is negligible for typical apps but worth knowing about if you have thousands of routes.

The default here optimizes for *seeing everything while you learn*. Turn the knobs down before you ship.

---

## Project structure

```text
observability-starter/
├── app/
│   ├── main.py                 # FastAPI app factory + endpoints
│   ├── config.py               # env-driven settings (pydantic-settings)
│   ├── services.py             # business logic with manual spans + business metric
│   └── telemetry/              # <- the reusable package
│       ├── tracing.py          #    OTel SDK, OTLP exporter, auto-instrumentation
│       ├── metrics.py          #    Prometheus RED metrics + ASGI middleware
│       └── logging.py          #    structlog JSON + trace correlation + request-id
├── deploy/
│   ├── docker-compose.yml      # app + collector + prometheus + tempo + grafana
│   ├── Dockerfile              # the app image
│   ├── otel-collector-config.yaml
│   ├── prometheus.yml          # scrape config (exemplar storage enabled)
│   ├── tempo.yaml              # single-binary Tempo, local storage
│   └── grafana/provisioning/   # datasources + dashboard, provisioned on boot
├── load/generate.py            # asyncio + httpx load generator
├── tests/                      # metric registration, spans, log/trace correlation
├── requirements.txt
└── requirements-dev.txt
```

---

## Related projects

Part of a set of production-shaped starters:

- **[docker-compose-stacks](https://github.com/AleBrito124356/docker-compose-stacks)** — copy-paste Docker Compose stacks for a dev machine; the observability stack here follows those conventions.
- **[ml-deployment-patterns](https://github.com/AleBrito124356/ml-deployment-patterns)** — serve sklearn/ONNX with FastAPI, versioning and drift; the exact kind of service you would bolt this telemetry onto.
- **[fastapi-production-template](https://github.com/AleBrito124356/fastapi-production-template)** — async SQLAlchemy, JWT, Redis and CI in a FastAPI starter, ready for this package to drop in.
- **[llm-gateway](https://github.com/AleBrito124356/llm-gateway)** — an OpenAI-compatible gateway with caching, routing and cost accounting, where per-request tracing earns its keep.

---

## License

MIT © 2026 Alejandro Brito. See [LICENSE](LICENSE).
