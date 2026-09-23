"""Runtime configuration.

Every field maps to an environment variable of the same name in upper case
(``service_name`` -> ``SERVICE_NAME``). Values can also live in a local ``.env``
file. See ``.env.example`` for the full list and where each value comes from.

Settings are validated when they are loaded, so a typo such as
``TRACE_SAMPLE_RATIO=1.5`` or ``OTEL_TRACES_EXPORTER=otpl`` stops the service at
startup with a message that names the variable, instead of being silently
accepted or crashing later deep inside the OpenTelemetry SDK.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__

#: Exporter names understood by ``OTEL_TRACES_EXPORTER``. These are the values
#: from the OpenTelemetry SDK environment-variable spec that this service
#: implements.
SUPPORTED_EXPORTERS = ("otlp", "console", "none")

#: Default collector endpoints per OTLP protocol (the spec's defaults).
DEFAULT_OTLP_ENDPOINTS = {
    "grpc": "http://localhost:4317",
    "http/protobuf": "http://localhost:4318",
}

_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def parse_exporter_list(value: str, *, variable: str) -> tuple[str, ...]:
    """Parse an ``OTEL_*_EXPORTER`` value into a tuple of exporter names.

    Accepts a comma-separated list (``"otlp,console"``). ``"none"`` means "no
    exporter" and cannot be combined with anything else. Unknown names raise a
    ``ValueError`` that names the variable and the accepted values.
    """

    names = tuple(part.strip().lower() for part in str(value).split(",") if part.strip())
    if not names:
        raise ValueError(f"{variable} is empty; use one of {', '.join(SUPPORTED_EXPORTERS)}")
    unknown = [name for name in names if name not in SUPPORTED_EXPORTERS]
    if unknown:
        raise ValueError(
            f"{variable}={value!r}: unsupported exporter(s) {', '.join(unknown)}; "
            f"supported values are {', '.join(SUPPORTED_EXPORTERS)} (comma-separated)"
        )
    if "none" in names and len(names) > 1:
        raise ValueError(f"{variable}={value!r}: 'none' cannot be combined with other exporters")
    if names == ("none",):
        return ()
    # Keep the order, drop duplicates.
    return tuple(dict.fromkeys(names))


class Settings(BaseSettings):
    """Application settings, populated from the environment or a ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Service identity (becomes OpenTelemetry Resource attributes) ---
    service_name: str = Field(default="observability-starter", min_length=1)
    service_version: str = __version__
    environment: str = "local"

    # --- OTLP export ---
    # Collector base URL. Left unset, it defaults per protocol to the spec's
    # http://localhost:4317 (grpc) or http://localhost:4318 (http/protobuf).
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_protocol: Literal["grpc", "http/protobuf"] = "grpc"
    # gRPC only: plaintext connection to the collector (no TLS).
    otel_exporter_otlp_insecure: bool = True
    # Comma-separated list of: otlp, console, none. "none" builds spans locally
    # without shipping them anywhere (tests, offline runs).
    otel_traces_exporter: str = "otlp"
    # Head-based sampling ratio. 1.0 keeps every trace; lower it in production.
    trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    # --- Logging ---
    log_level: str = "INFO"
    log_json: bool = True
    # One structured, trace-correlated "request.completed" line per request.
    # (Run uvicorn with --no-access-log to avoid its plain-text duplicate.)
    log_requests: bool = True

    # --- Demo behaviour knobs ---
    # URL the /api/external endpoint calls to exercise httpx instrumentation.
    upstream_url: str = "http://localhost:8000/health"
    # Simulated dependency latency window, in milliseconds.
    slow_min_ms: int = Field(default=20, ge=0)
    slow_max_ms: int = Field(default=400, ge=0)
    # Fraction of order reads that fail, so error traces and metrics show up.
    failure_rate: float = Field(default=0.1, ge=0.0, le=1.0)

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = str(value).strip().upper()
        if level == "WARN":
            level = "WARNING"
        if level not in _LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL={value!r} is not one of {', '.join(_LOG_LEVELS)}")
        return level

    @field_validator("otel_traces_exporter")
    @classmethod
    def _check_traces_exporter(cls, value: str) -> str:
        parse_exporter_list(value, variable="OTEL_TRACES_EXPORTER")
        return value.strip().lower()

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _check_endpoint(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                f"OTEL_EXPORTER_OTLP_ENDPOINT={value!r} must start with http:// or https://"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_latency_window(self) -> Settings:
        if self.slow_min_ms > self.slow_max_ms:
            raise ValueError(
                f"SLOW_MIN_MS ({self.slow_min_ms}) must be <= SLOW_MAX_MS ({self.slow_max_ms})"
            )
        return self

    # --- Derived values -------------------------------------------------------

    @property
    def traces_exporters(self) -> tuple[str, ...]:
        """The parsed ``OTEL_TRACES_EXPORTER`` list (empty tuple for ``none``)."""

        return parse_exporter_list(self.otel_traces_exporter, variable="OTEL_TRACES_EXPORTER")

    @property
    def otlp_endpoint(self) -> str:
        """The collector base URL, defaulted per protocol when unset."""

        return self.otel_exporter_otlp_endpoint or DEFAULT_OTLP_ENDPOINTS[
            self.otel_exporter_otlp_protocol
        ]

    @property
    def log_level_number(self) -> int:
        """``log_level`` as a ``logging`` module constant."""

        return logging.getLevelName(self.log_level)


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""

    return Settings()
