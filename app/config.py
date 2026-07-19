"""Runtime configuration.

Every field maps to an environment variable of the same name in upper case
(``service_name`` -> ``SERVICE_NAME``). Values can also live in a local ``.env``
file. See ``.env.example`` for the full list and where each value comes from.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, populated from the environment or a ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Service identity (becomes OpenTelemetry Resource attributes) ---
    service_name: str = "observability-starter"
    service_version: str = "0.1.0"
    environment: str = "local"

    # --- Tracing / OTLP export ---
    # Standard OpenTelemetry variable. Points at the collector's OTLP gRPC port.
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_exporter_otlp_insecure: bool = True
    # Set to "none" to build spans without shipping them anywhere (used by tests).
    otel_traces_exporter: str = "otlp"
    # Head-based sampling ratio. 1.0 keeps every trace; lower it in production.
    trace_sample_ratio: float = 1.0

    # --- Logging ---
    log_level: str = "INFO"
    log_json: bool = True

    # --- Demo behaviour knobs ---
    # URL the /api/external endpoint calls to exercise httpx instrumentation.
    upstream_url: str = "http://localhost:8000/health"
    # Simulated dependency latency window, in milliseconds.
    slow_min_ms: int = 20
    slow_max_ms: int = 400
    # Fraction of order reads that fail, so error traces and metrics show up.
    failure_rate: float = 0.1


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""

    return Settings()
