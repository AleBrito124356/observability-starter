"""Stack contract tests: deploy/ must stay consistent with itself and with the app.

Docker is not needed. These tests read the compose file, the collector, Tempo,
Loki and Prometheus configs, the Grafana provisioning and the dashboard, and
check the wiring a typo would silently break: metric names used by the
dashboard, datasource uids, scrape targets, collector pipelines, exporter
endpoints, image pins and the Grafana macro escaping.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from prometheus_client import REGISTRY

# Importing these registers the app's metrics in the default registry.
import app.business_metrics
import app.telemetry.metrics  # noqa: F401

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
PROVISIONING = DEPLOY / "grafana" / "provisioning"

# Metrics that exist in Prometheus without being defined by the app.
EXTERNAL_METRIC_PREFIXES = ("traces_service_graph_", "traces_spanmetrics_", "otelcol_", "up")

PROMQL_WORDS = {
    # functions and aggregation operators used (or likely to be used) in panels
    "sum", "avg", "min", "max", "count", "rate", "irate", "increase", "delta",
    "histogram_quantile", "topk", "bottomk", "abs", "clamp_min", "clamp_max",
    "label_replace", "vector", "scalar", "time", "round", "sort", "sort_desc",
    # keywords
    "by", "without", "on", "ignoring", "group_left", "group_right", "offset",
    "bool", "and", "or", "unless",
}  # fmt: skip


def _yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict:
    return _yaml(DEPLOY / "docker-compose.yml")


@pytest.fixture(scope="module")
def collector() -> dict:
    return _yaml(DEPLOY / "otel-collector-config.yaml")


@pytest.fixture(scope="module")
def datasources() -> list[dict]:
    return _yaml(PROVISIONING / "datasources" / "datasources.yaml")["datasources"]


@pytest.fixture(scope="module")
def dashboards() -> list[dict]:
    folder = PROVISIONING / "dashboards"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def _targets(dashboard: dict):
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            yield panel, target


def _panel_uid(panel: dict, target: dict) -> str:
    return (target.get("datasource") or panel.get("datasource") or {}).get("uid", "")


def _uids_by_type(datasources) -> dict[str, str]:
    return {ds["uid"]: ds["type"] for ds in datasources}


def promql_metric_names(expr: str) -> set[str]:
    """Pull the metric names out of a PromQL expression (panel-sized parser)."""

    expr = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)  # string literals
    expr = re.sub(r"\{[^}]*\}", "", expr)  # label matchers
    expr = re.sub(r"\[[^\]]*\]", "", expr)  # range selectors
    expr = re.sub(r"\b(by|without|on|ignoring)\s*\([^)]*\)", "", expr)  # grouping labels
    names = set()
    for match in re.finditer(r"[A-Za-z_:][A-Za-z0-9_:]*", expr):
        word = match.group(0)
        following = expr[match.end() :].lstrip()
        if word in PROMQL_WORDS or following.startswith("("):
            continue
        if word.startswith("$"):
            continue
        names.add(word)
    return names


# --- The parser itself ---------------------------------------------------------


def test_promql_parser_finds_metric_names():
    expr = (
        'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
        '{path=~"/api.*"}[$__rate_interval])) by (le)) / sum(http_requests_in_progress)'
    )
    assert promql_metric_names(expr) == {
        "http_request_duration_seconds_bucket",
        "http_requests_in_progress",
    }


# --- Dashboard <-> app ------------------------------------------------------------


def test_every_dashboard_metric_exists_in_the_app_registry(dashboards, datasources):
    uid_types = _uids_by_type(datasources)
    registered = set(REGISTRY._names_to_collectors)
    missing = []
    for dashboard in dashboards:
        for panel, target in _targets(dashboard):
            if uid_types.get(_panel_uid(panel, target)) != "prometheus":
                continue
            for name in promql_metric_names(target["expr"]):
                if name not in registered and not name.startswith(EXTERNAL_METRIC_PREFIXES):
                    missing.append((panel["title"], name))
    assert missing == [], f"dashboard queries metrics the app does not expose: {missing}"


def test_the_latency_panel_asks_for_exemplars(dashboards):
    exemplar_targets = [
        target
        for dashboard in dashboards
        for _, target in _targets(dashboard)
        if target.get("exemplar")
    ]
    assert exemplar_targets, "no panel requests exemplars; the metrics -> trace hop is gone"
    assert all("http_request_duration_seconds_bucket" in t["expr"] for t in exemplar_targets)


def test_loki_queries_select_the_service_the_app_reports(dashboards, datasources, compose):
    uid_types = _uids_by_type(datasources)
    service_name = compose["services"]["app"]["environment"]["SERVICE_NAME"]
    loki_exprs = [
        target["expr"]
        for dashboard in dashboards
        for panel, target in _targets(dashboard)
        if uid_types.get(_panel_uid(panel, target)) == "loki"
    ]
    assert loki_exprs, "the dashboard has no log panel"
    for expr in loki_exprs:
        assert f'service_name="{service_name}"' in expr


def test_panel_ids_are_unique(dashboards):
    for dashboard in dashboards:
        ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(ids) == len(set(ids))


# --- Datasources ------------------------------------------------------------------


def test_every_referenced_datasource_uid_is_provisioned(dashboards, datasources):
    uid_types = _uids_by_type(datasources)
    referenced = set()
    for dashboard in dashboards:
        for panel in dashboard["panels"]:
            if "datasource" in panel:
                referenced.add(panel["datasource"]["uid"])
            for target in panel.get("targets", []):
                if "datasource" in target:
                    referenced.add(target["datasource"]["uid"])
    for ds in datasources:
        data = ds.get("jsonData", {})
        for link in data.get("exemplarTraceIdDestinations", []):
            referenced.add(link["datasourceUid"])
        for field in data.get("derivedFields", []):
            referenced.add(field["datasourceUid"])
        for key in ("serviceMap", "tracesToMetrics", "lokiSearch", "tracesToLogsV2"):
            if key in data:
                referenced.add(data[key]["datasourceUid"])
    unknown = referenced - set(uid_types) - {"-- Grafana --"}
    assert unknown == set(), f"unprovisioned datasource uids: {unknown}"


def test_every_pivot_link_points_at_the_right_kind_of_datasource(datasources):
    uid_types = _uids_by_type(datasources)
    by_type = {ds["type"]: ds for ds in datasources}
    prometheus, tempo, loki = by_type["prometheus"], by_type["tempo"], by_type["loki"]

    # metrics -> trace
    destinations = prometheus["jsonData"]["exemplarTraceIdDestinations"]
    assert [uid_types[d["datasourceUid"]] for d in destinations] == ["tempo"]
    assert destinations[0]["name"] == "trace_id"  # the exemplar label the app emits

    # trace -> logs, trace -> service graph
    tempo_data = tempo["jsonData"]
    assert uid_types[tempo_data["tracesToLogsV2"]["datasourceUid"]] == "loki"
    assert "trace_id=" in tempo_data["tracesToLogsV2"]["query"]
    assert uid_types[tempo_data["serviceMap"]["datasourceUid"]] == "prometheus"

    # logs -> trace
    (derived,) = loki["jsonData"]["derivedFields"]
    assert uid_types[derived["datasourceUid"]] == "tempo"
    assert derived["matcherType"] == "label" and derived["matcherRegex"] == "trace_id"


def test_grafana_macros_are_escaped_from_env_expansion():
    text = (PROVISIONING / "datasources" / "datasources.yaml").read_text(encoding="utf-8")
    # Grafana expands ${VAR} in provisioning files; macros must be written $${...}.
    unescaped = re.findall(r"(?<!\$)\$\{__[^}]*\}", text)
    assert unescaped == []
    assert "$${__trace.traceId}" in text


def test_datasource_urls_point_at_compose_services(datasources, compose):
    listening = _listening_ports(compose)
    for ds in datasources:
        url = urlparse(ds["url"])
        assert url.hostname in compose["services"], ds["name"]
        assert url.port in listening[url.hostname], (ds["name"], url.port)


# --- Compose, scrape targets and the collector ----------------------------------------


def _listening_ports(compose: dict) -> dict[str, set[int]]:
    """Ports each service listens on inside the compose network."""

    ports: dict[str, set[int]] = {name: set() for name in compose["services"]}
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []):
            ports[name].add(int(str(mapping).split(":")[-1].split("/")[0]))
    # Ports declared in the services' own configs rather than published.
    tempo = _yaml(DEPLOY / "tempo.yaml")
    for protocol in tempo["distributor"]["receivers"]["otlp"]["protocols"].values():
        ports["tempo"].add(int(protocol["endpoint"].rsplit(":", 1)[1]))
    ports["tempo"].add(tempo["server"]["http_listen_port"])
    ports["loki"].add(_yaml(DEPLOY / "loki.yaml")["server"]["http_listen_port"])
    receivers = _yaml(DEPLOY / "otel-collector-config.yaml")["receivers"]["otlp"]["protocols"]
    for protocol in receivers.values():
        ports["otel-collector"].add(int(protocol["endpoint"].rsplit(":", 1)[1]))
    return ports


def _host_port(endpoint: str) -> tuple[str, int]:
    if "://" not in endpoint:
        endpoint = "tcp://" + endpoint
    parsed = urlparse(endpoint)
    return parsed.hostname, parsed.port


def test_prometheus_scrape_targets_resolve_to_compose_services(compose):
    listening = _listening_ports(compose)
    config = _yaml(DEPLOY / "prometheus.yml")
    targets = [
        target
        for job in config["scrape_configs"]
        for static in job["static_configs"]
        for target in static["targets"]
    ]
    assert "app:8000" in targets
    for target in targets:
        host, port = target.rsplit(":", 1)
        assert host in compose["services"], target
        assert int(port) in listening[host], target


def test_collector_pipelines_use_only_declared_components(collector):
    kinds = ("receivers", "processors", "exporters")
    declared = {kind: set(collector.get(kind, {})) for kind in kinds}
    pipelines = collector["service"]["pipelines"]
    assert {"traces", "logs"} <= set(pipelines)
    for name, pipeline in pipelines.items():
        for kind in ("receivers", "processors", "exporters"):
            unknown = set(pipeline.get(kind, [])) - declared[kind]
            assert unknown == set(), f"pipeline {name} uses undeclared {kind}: {unknown}"
    assert set(collector["service"].get("extensions", [])) <= set(collector.get("extensions", {}))


def test_collector_exporters_point_at_compose_services(collector, compose):
    listening = _listening_ports(compose)
    for name, exporter in collector["exporters"].items():
        if "endpoint" not in exporter:
            continue
        host, port = _host_port(exporter["endpoint"])
        assert host in compose["services"], name
        assert port in listening[host], (name, port)
    assert "loki" in " ".join(collector["service"]["pipelines"]["logs"]["exporters"])
    assert collector["exporters"]["otlphttp/loki"]["endpoint"].endswith("/otlp")


def test_the_app_ships_traces_and_logs_to_the_collector(compose):
    env = compose["services"]["app"]["environment"]
    host, port = _host_port(env["OTEL_EXPORTER_OTLP_ENDPOINT"])
    assert host == "otel-collector"
    expected_port = 4317 if env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc") == "grpc" else 4318
    assert port == expected_port
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"


def test_app_environment_is_valid_for_the_settings_model(compose):
    from app.config import Settings

    env = {k.lower(): v for k, v in compose["services"]["app"]["environment"].items()}
    settings = Settings(_env_file=None, **env)
    assert settings.logs_exporters == ("otlp",)


def test_tempo_remote_writes_to_a_prometheus_that_accepts_it(compose):
    tempo = _yaml(DEPLOY / "tempo.yaml")
    (remote,) = tempo["metrics_generator"]["storage"]["remote_write"]
    url = urlparse(remote["url"])
    assert url.hostname == "prometheus" and url.path == "/api/v1/write"
    assert "--web.enable-remote-write-receiver" in compose["services"]["prometheus"]["command"]
    processors = tempo["overrides"]["defaults"]["metrics_generator"]["processors"]
    assert "service-graphs" in processors


def test_loki_accepts_otlp_structured_metadata():
    loki = _yaml(DEPLOY / "loki.yaml")
    assert loki["limits_config"]["allow_structured_metadata"] is True
    schema = loki["schema_config"]["configs"][-1]
    assert schema["store"] == "tsdb" and schema["schema"] == "v13"


def test_prometheus_keeps_exemplars(compose):
    assert "--enable-feature=exemplar-storage" in compose["services"]["prometheus"]["command"]


def test_third_party_images_are_pinned(compose):
    for name, service in compose["services"].items():
        if "build" in service:
            continue  # the locally built app image
        image = service["image"]
        repository, _, tag = image.rpartition(":")
        assert repository and tag and tag != "latest", f"{name} uses an unpinned image: {image}"


def test_depends_on_names_real_services(compose):
    for name, service in compose["services"].items():
        for dependency in service.get("depends_on", []):
            assert dependency in compose["services"], (name, dependency)


def test_mounted_config_files_exist(compose):
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            source = str(volume).split(":")[0]
            if source.startswith("./"):
                assert (DEPLOY / source).exists(), (name, source)
