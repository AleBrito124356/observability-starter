# Changelog

## 0.2.0

### Added

- **Offline pivot demo** — `observability-starter demo` / `python -m app.demo` runs the real app in-process and prints the RED table (quantiles via a port of PromQL's `histogram_quantile`), the exemplar on the slowest latency bucket and on the slowest 5xx bucket, those traces as ASCII waterfalls, and their correlated JSON log lines. `--json` gives a machine-readable report.
- **Logs pillar in the stack** — OTLP log export (`OTEL_LOGS_EXPORTER=otlp|console|none`, default `none`) through a stdlib/structlog → OpenTelemetry bridge that keeps `trace_id`/`span_id`; Loki 3.2 in docker-compose behind a collector logs pipeline; Grafana trace → logs (`tracesToLogsV2`) and logs → trace (`derivedFields`) links; "Logs for this service", WARN+ log rate and service graph panels.
- **Service graph** — Tempo's metrics generator (service-graphs, span-metrics) remote-writes to Prometheus, started with `--web.enable-remote-write-receiver`.
- `OTEL_EXPORTER_OTLP_PROTOCOL=grpc|http/protobuf` for traces and logs; the endpoint defaults to 4317 or 4318 accordingly.
- `app.telemetry.setup_telemetry(app, settings, ...)`: one call that wires logging, request id, metrics and tracing in the order correlation needs, returning a `Telemetry` handle with `force_flush()` / `shutdown()`.
- A structured `request.completed` access-log line per request (method, path, route template, status, duration, trace id), controlled by `LOG_REQUESTS` (default `true`).
- Packaging: `pip install -e ".[dev]"` works (build system, dependencies, `dev` extra) and installs the `observability-starter` CLI with `serve`, `demo` and `load` subcommands.
- Settings validation with messages that name the variable (`TRACE_SAMPLE_RATIO` and `FAILURE_RATE` in `[0, 1]`, `SLOW_MIN_MS <= SLOW_MAX_MS`, real log levels, known exporter names, http(s) endpoints).
- 136 new tests (148 in total, all offline), including OTLP end-to-end tests against fake gRPC and HTTP collectors and contract tests over `deploy/`.

### Fixed

- `GET /metrics` answered `307 → /metrics/` with an empty body (it was an `app.mount`), so `curl /metrics | grep` printed nothing and scrapers that do not follow redirects got no data. It is now a real route at `/metrics` and `/metrics/` that keeps OpenMetrics negotiation, gzip and `?name[]=` filtering.
- Exemplars were attached to unsampled traces, so with `TRACE_SAMPLE_RATIO < 1` most exemplar links pointed at traces that were never exported. Only sampled spans get an exemplar now.
- The `method` label used the raw request method, so `PROPFIND`, `PURGE` or (with `--http h11`) any token created new series. Non-standard methods are recorded as `_OTHER`.
- `X-Request-ID` was trusted verbatim (a 6000-character value was echoed in the response header and written to every log line). Only `[A-Za-z0-9._:-]{1,128}` is accepted; anything else is replaced.
- `OTEL_TRACES_EXPORTER=console` silently built an OTLP exporter; unknown values now fail fast.
- `TRACE_SAMPLE_RATIO=1.5` crashed inside the OTel SDK at import; `FAILURE_RATE=7` was accepted.
- `process_order` and `external.aggregate` recorded their exception twice and the SDK overwrote the status message.
- A request served in-process from inside another one wiped the outer request's id from the log context.
- `configure_tracing` silently dropped `span_processors` when a provider already existed, and the lifespan shut down providers it did not create.

### Changed

- `ORDERS_PROCESSED` moved from `app/telemetry/metrics.py` to `app/business_metrics.py`, so the reusable package no longer contains demo code. Update imports if you used it.
- `import app.main` no longer builds the app; the module-level `app` (used by `uvicorn app.main:app`) is created on first access.
- `/api/external` uses one app-scoped `httpx.AsyncClient` created in the lifespan instead of a client per request.
- `UPSTREAM_URL` defaults to `http://localhost:8000/` (the traced root endpoint) instead of `/health`, so the self-call shows a propagated SERVER span and a service-graph edge. `observability-starter serve` points it at its own port unless you set it.
- While `LOG_REQUESTS=true`, uvicorn's plain-text access log is silenced (the app's structured line replaces it); set `LOG_REQUESTS=false` to get uvicorn's lines, as JSON.
- The per-message ASGI `http send` / `http receive` spans are no longer created (two or three uninformative spans per request).
- `opentelemetry-instrumentation-logging` is no longer used: since 0.65 it injected nothing without extra flags and installed a second root handler bound to the global `LoggerProvider`; trace ids already reach stdlib records through the structlog chain.
- Minimum versions: `opentelemetry-*` 1.35.0 and instrumentation 0.56b0 (the first SDK whose `LogRecord` takes a `context`). The suite is tested on 1.35.0/0.56b0 and 1.44.0/0.65b0.
- New dependency: `opentelemetry-exporter-otlp-proto-http`. New dev dependencies: `pyyaml`, `ruff`.

## 0.1.0

- Initial release: FastAPI service with OpenTelemetry tracing, Prometheus RED metrics with exemplars, structlog JSON logs with trace correlation, and a docker-compose stack of collector, Prometheus, Tempo and Grafana.
