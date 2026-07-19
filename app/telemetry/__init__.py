"""The reusable telemetry package.

Copy this folder into your own FastAPI service and wire it up in three calls::

    from app.telemetry.logging import configure_logging, RequestIDMiddleware
    from app.telemetry.metrics import PrometheusMiddleware
    from app.telemetry.tracing import configure_tracing

    configure_logging(settings)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(PrometheusMiddleware)
    app.mount("/metrics", make_asgi_app())
    configure_tracing(app, settings)

Nothing in here is specific to the demo endpoints; the only project coupling is
the business counter ``ORDERS_PROCESSED`` in ``metrics.py``, which you would
rename for your own domain.
"""
