"""MCP-сервер мониторинга: метрики, оповещения и агрегированные ошибки.

Это настоящий сервер, а не обёртка, которую импортирует агент. Он работает
отдельным процессом, говорит на MCP поверх stdio, и агент обращается к нему
только через протокол. В этом разделении и смысл: замена синтетического набора
данных за ним на Prometheus и хранилище логов меняет этот файл и ничего в
агенте.

То, что он намеренно не раскрывает, — это сырые строки логов.
get_error_groups агрегирует перед возвратом, потому что слой интеграции,
способный отдать неограниченный объём, — это слой, который в конце концов его
отдаст.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.adapters.mock.dataset import DEFAULT_SCENARIO, Scenario, aggregate_logs
from app.mcp_servers.common import (
    ToolFailure,
    iso,
    parse_window,
    scenario_from_env,
    services_in,
)


class MetricPointOut(BaseModel):
    at: str
    value: float


class MetricsOut(BaseModel):
    """И выборки, и предвычисленная сводка.

    Сводка существует, чтобы вызывающему, которому нужно лишь «стало ли хуже»,
    не приходилось передавать или анализировать весь ряд.
    """

    service: str
    metric: str
    unit: str
    sample_count: int
    first_value: float | None = None
    last_value: float | None = None
    mean: float = 0.0
    peak_value: float | None = None
    peak_at: str | None = None
    points: list[MetricPointOut] = Field(default_factory=list)


class AlertOut(BaseModel):
    name: str
    service: str
    severity: str
    fired_at: str
    resolved_at: str | None = None
    description: str = ""


class ErrorGroupOut(BaseModel):
    error_type: str
    count: int
    first_seen: str
    last_seen: str
    sample_message: str
    stack_top: str | None = None
    services: list[str] = Field(default_factory=list)


class ServiceInfo(BaseModel):
    name: str
    metrics: list[str]


def build_server(scenario: Scenario = DEFAULT_SCENARIO) -> MCPServer:
    """Создаёт сервер поверх заданного хранилища.

    Параметризовано, чтобы тесты могли подключить к нему клиент в том же
    процессе, со сценарием на свой выбор, а не порождать подпроцесс.
    """
    server = MCPServer(
        name="ops-monitoring",
        version="1.0.0",
        instructions=(
            "Read-only monitoring for backend services: metric time series, "
            "firing alerts, and application errors aggregated by type. "
            "All timestamps are UTC ISO 8601."
        ),
    )

    @server.tool(
        description=(
            "Fetch one metric time series for a service over a window. Returns the "
            "samples together with mean, peak and peak timestamp. Known metrics are "
            "error_rate, request_rate, latency_p50, latency_p95 and latency_p99."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_service_metrics(service: str, metric: str, start: str, end: str) -> MetricsOut:
        window = parse_window(start, end)
        series = scenario.metrics.get((service, metric))
        if series is None:
            raise ToolFailure(
                f"no series {metric!r} for service {service!r}; "
                f"known services: {', '.join(services_in(scenario))}"
            )
        points = [p for p in series.points if window[0] <= p.timestamp <= window[1]]
        peak = max(points, key=lambda p: p.value, default=None)
        return MetricsOut(
            service=series.service,
            metric=series.metric,
            unit=series.unit,
            sample_count=len(points),
            first_value=points[0].value if points else None,
            last_value=points[-1].value if points else None,
            mean=sum(p.value for p in points) / len(points) if points else 0.0,
            peak_value=peak.value if peak else None,
            peak_at=iso(peak.timestamp) if peak else None,
            points=[MetricPointOut(at=iso(p.timestamp), value=p.value) for p in points],
        )

    @server.tool(
        description=(
            "The current error ratio for a service: the mean of error_rate over the "
            "window, as a fraction of requests. A convenience over get_service_metrics "
            "for the common question."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_error_rate(service: str, start: str, end: str) -> float:
        window = parse_window(start, end)
        series = scenario.metrics.get((service, "error_rate"))
        if series is None:
            raise ToolFailure(f"no error_rate series for service {service!r}")
        points = [p for p in series.points if window[0] <= p.timestamp <= window[1]]
        return round(sum(p.value for p in points) / len(points), 6) if points else 0.0

    @server.tool(
        description=(
            "Alerts that fired for a service in the window, with severity, the time "
            "they fired and the condition that triggered them."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_recent_alerts(service: str, start: str, end: str) -> list[AlertOut]:
        window = parse_window(start, end)
        return [
            AlertOut(
                name=a.name,
                service=a.service,
                severity=str(a.severity),
                fired_at=iso(a.fired_at),
                resolved_at=iso(a.resolved_at) if a.resolved_at else None,
                description=a.description,
            )
            for a in scenario.alerts
            if a.service == service and window[0] <= a.fired_at <= window[1]
        ]

    @server.tool(
        description=(
            "Application errors in the window, aggregated by error type: count, first "
            "and last occurrence, one sample message and the failing stack frame. Raw "
            "log lines are never returned — use min_count to drop long-tail noise."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_error_groups(
        service: str, start: str, end: str, min_count: int = 1
    ) -> list[ErrorGroupOut]:
        window = parse_window(start, end)
        if min_count < 1:
            raise ToolFailure("min_count must be at least 1")
        events = [e for e in scenario.logs if e.service == service]
        return [
            ErrorGroupOut(
                error_type=g.error_type,
                count=g.count,
                first_seen=iso(g.first_seen),
                last_seen=iso(g.last_seen),
                sample_message=g.sample_message,
                stack_top=g.stack_top,
                services=list(g.services),
            )
            for g in aggregate_logs(events, window[0], window[1], min_count)
        ]

    @server.resource(
        "monitoring://services",
        name="Monitored services",
        description="Which services this monitoring backend knows, and their metrics.",
        mime_type="application/json",
    )
    def services() -> list[ServiceInfo]:
        by_service: dict[str, list[str]] = {}
        for svc, metric in scenario.metrics:
            by_service.setdefault(svc, []).append(metric)
        return [
            ServiceInfo(name=name, metrics=sorted(metrics))
            for name, metrics in sorted(by_service.items())
        ]

    return server


def main() -> None:
    build_server(scenario_from_env()).run("stdio")


if __name__ == "__main__":
    main()
