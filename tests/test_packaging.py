"""Packaging metadata and the ``observability-starter`` CLI."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import pytest

import app
from app import cli

ROOT = Path(__file__).resolve().parents[1]


def _requirement_lines(path: Path) -> set[str]:
    lines = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-r"):
            lines.add(line)
    return lines


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_and_requirements_declare_the_same_runtime_deps(pyproject):
    assert set(pyproject["project"]["dependencies"]) == _requirement_lines(
        ROOT / "requirements.txt"
    )


def test_dev_extra_matches_requirements_dev(pyproject):
    assert set(pyproject["project"]["optional-dependencies"]["dev"]) == _requirement_lines(
        ROOT / "requirements-dev.txt"
    )


def test_version_is_single_sourced(pyproject):
    assert pyproject["project"]["version"] == app.__version__
    from app.config import Settings

    assert Settings(_env_file=None).service_version == app.__version__


def test_declared_packages_exist_and_the_script_resolves(pyproject):
    for package in pyproject["tool"]["setuptools"]["packages"]:
        assert (ROOT / package.replace(".", "/") / "__init__.py").exists(), package
    module, _, attribute = pyproject["project"]["scripts"]["observability-starter"].partition(":")
    assert module == "app.cli" and callable(getattr(cli, attribute))


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    expected = rf"observability-starter {re.escape(app.__version__)}\s*"
    assert re.fullmatch(expected, capsys.readouterr().out)


def test_cli_demo_subcommand(capsys, monkeypatch):
    monkeypatch.setenv("SLOW_MIN_MS", "1")
    monkeypatch.setenv("SLOW_MAX_MS", "10")
    assert cli.main(["demo", "--requests", "20", "--seed", "4", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requests_sent"] == 20


def test_cli_load_subcommand_validates(capsys):
    assert cli.main(["load", "--duration", "-1"]) == 2


@pytest.fixture()
def fake_uvicorn(monkeypatch, tmp_path):
    """Capture uvicorn.run instead of serving; isolate os.environ and .env."""

    import uvicorn

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(os, "environ", {k: v for k, v in os.environ.items()})
    monkeypatch.chdir(tmp_path)  # no stray .env
    os.environ.pop("UPSTREAM_URL", None)
    return calls


def test_serve_runs_uvicorn_on_the_app(fake_uvicorn):
    assert cli.main(["serve", "--port", "8123"]) == 0
    ((args, kwargs),) = fake_uvicorn
    assert args == ("app.main:app",)
    assert kwargs == {"host": "127.0.0.1", "port": 8123, "reload": False}
    # /api/external self-calls the port actually being served.
    assert os.environ["UPSTREAM_URL"] == "http://127.0.0.1:8123/"


def test_serve_keeps_an_explicit_upstream(fake_uvicorn):
    os.environ["UPSTREAM_URL"] = "http://inventory:9000/"
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9001", "--reload"]) == 0
    assert os.environ["UPSTREAM_URL"] == "http://inventory:9000/"
    assert fake_uvicorn[0][1]["reload"] is True


def test_serve_refuses_invalid_settings(fake_uvicorn, capsys):
    os.environ["TRACE_SAMPLE_RATIO"] = "5"
    assert cli.main(["serve"]) == 2
    assert fake_uvicorn == []
    assert "trace_sample_ratio" in capsys.readouterr().err
