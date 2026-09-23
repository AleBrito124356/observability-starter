"""observability-starter: the three pillars of observability wired into a FastAPI service.

Logs, metrics and traces on a small-but-real service, exported to a docker-compose
stack of OpenTelemetry Collector, Prometheus, Tempo, Loki and Grafana. The
reusable part is ``app.telemetry`` (``setup_telemetry``); ``app.demo`` walks the
metrics -> exemplar -> trace -> logs pivot offline.
"""

__version__ = "0.2.0"
