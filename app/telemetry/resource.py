"""The OpenTelemetry Resource shared by every signal this service exports."""

from __future__ import annotations

from opentelemetry.sdk.resources import Resource


def build_resource(settings) -> Resource:
    """Tag spans and log records with the service identity.

    ``service.name`` is what Tempo groups traces by and what Loki turns into
    the ``service_name`` stream label, so both backends agree on it.
    """

    return Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        }
    )
