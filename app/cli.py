"""The ``observability-starter`` command line.

    observability-starter serve [--host H] [--port P] [--reload]
    observability-starter demo  [--requests N] [--concurrency C] [--seed S] [--json]
    observability-starter load  [--base-url URL] [--duration S] [--concurrency C]

``serve`` validates the settings first, so a bad ``.env`` stops with a short
message instead of a traceback from inside uvicorn.
"""

from __future__ import annotations

import argparse
import os
import sys

from pydantic import ValidationError

from app import __version__


def _serve(args: argparse.Namespace) -> int:
    from app.config import Settings

    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"invalid configuration:\n{exc}", file=sys.stderr)
        return 2

    # /api/external calls the service itself by default. When neither the
    # environment nor .env says otherwise, point it at the port we serve on
    # (the built-in default assumes 8000).
    if "upstream_url" not in settings.model_fields_set:
        host = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
        os.environ["UPSTREAM_URL"] = f"http://{host}:{args.port}/"

    import uvicorn

    # Access logging is decided by LOG_REQUESTS: the app's own structured
    # request.completed line by default, uvicorn's (as JSON) when it is off.
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    from app.demo import build_parser as build_demo_parser
    from load.generate import build_parser as build_load_parser

    parser = argparse.ArgumentParser(
        prog="observability-starter",
        description="Logs, metrics and traces for a FastAPI service - run, demo, load.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    serve = commands.add_parser("serve", help="Run the instrumented service with uvicorn.")
    serve.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    serve.add_argument("--port", type=int, default=8000, help="Port (default 8000).")
    serve.add_argument("--reload", action="store_true", help="Restart on code changes.")

    build_demo_parser(
        commands.add_parser(
            "demo",
            help="Offline pivot demo: metrics -> exemplar -> trace -> logs, in-process.",
        )
    )
    build_load_parser(
        commands.add_parser("load", help="Drive the weighted request mix at a running service.")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "demo":
        from app.demo import run_from_args

        return run_from_args(args)
    from load.generate import run_from_args

    return run_from_args(args)


if __name__ == "__main__":
    raise SystemExit(main())
