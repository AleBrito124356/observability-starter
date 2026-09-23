"""Parsing of the ``OTEL_*_EXPORTER`` environment-variable values.

Lives inside the telemetry package so the package stays self-contained;
``app.config`` reuses it to validate settings at load time.
"""

from __future__ import annotations

#: Exporter names understood by ``OTEL_TRACES_EXPORTER`` and
#: ``OTEL_LOGS_EXPORTER``. These are the values from the OpenTelemetry SDK
#: environment-variable spec that this package implements.
SUPPORTED_EXPORTERS = ("otlp", "console", "none")


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
