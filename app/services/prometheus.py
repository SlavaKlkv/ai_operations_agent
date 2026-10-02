"""Narrow, read-only boundary to a user-provided Prometheus HTTP API.

The agent never receives an arbitrary PromQL string from a task or a model.
Metric names and the service label are fixed here, and the service value is
escaped before it becomes a label matcher.  This keeps a real monitoring
backend useful without turning the integration into a general query console.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.domain.models import Alert, AlertSeverity, MetricPoint, MetricSeries


class PrometheusConnectionError(RuntimeError):
    """A safe, actionable error suitable for the local setup UI."""


METRICS: dict[str, tuple[str, str]] = {
    "error_rate": (
        'sum(rate(http_requests_total{SERVICE,status=~"5.."}[5m])) / '
        "sum(rate(http_requests_total{SERVICE}[5m]))",
        "ratio",
    ),
    "request_rate": ("sum(rate(http_requests_total{SERVICE}[5m]))", "rps"),
    "latency_p50": (
        "histogram_quantile(0.50, "
        "sum(rate(http_request_duration_seconds_bucket{SERVICE}[5m])) by (le))",
        "seconds",
    ),
    "latency_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(http_request_duration_seconds_bucket{SERVICE}[5m])) by (le))",
        "seconds",
    ),
    "latency_p99": (
        "histogram_quantile(0.99, "
        "sum(rate(http_request_duration_seconds_bucket{SERVICE}[5m])) by (le))",
        "seconds",
    ),
}


def _base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise PrometheusConnectionError("Укажите корректный адрес Prometheus: http://host:9090.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise PrometheusConnectionError("Адрес Prometheus не должен содержать путь или параметры.")
    return value.strip().rstrip("/")


def _label_value(value: str) -> str:
    if not value or len(value) > 200:
        raise PrometheusConnectionError(
            "Имя сервиса Prometheus должно содержать от 1 до 200 символов."
        )
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def _query(metric: str, service: str, label: str) -> tuple[str, str]:
    try:
        template, unit = METRICS[metric]
    except KeyError as exc:
        raise PrometheusConnectionError(f"Метрика {metric!r} не поддерживается.") from exc
    if not label.replace("_", "").isalnum():
        raise PrometheusConnectionError("Имя label Prometheus содержит недопустимые символы.")
    matcher = f'{label}="{_label_value(service)}"'
    return template.replace("SERVICE", matcher), unit


class PrometheusClient:
    """Only the API operations used by the monitoring provider."""

    def __init__(
        self,
        url: str,
        *,
        service_label: str = "service",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = _base_url(url)
        self.service_label = service_label
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def metric_range(
        self, service: str, metric: str, start: datetime, end: datetime
    ) -> MetricSeries:
        query, unit = _query(metric, service, self.service_label)
        if end <= start:
            raise PrometheusConnectionError("Окно метрик должно иметь положительную длительность.")
        payload = await self._request(
            "/api/v1/query_range",
            {
                "query": query,
                "start": str(start.astimezone(UTC).timestamp()),
                "end": str(end.astimezone(UTC).timestamp()),
                "step": str(max(15, min(300, int((end - start).total_seconds() / 120) or 15))),
            },
        )
        data = payload.get("data")
        results = data.get("result") if isinstance(data, dict) else None
        if not isinstance(results, list):
            raise PrometheusConnectionError("Prometheus вернул некорректный ряд метрик.")
        points: list[MetricPoint] = []
        for result in results:
            if not isinstance(result, dict):
                continue
            values = result.get("values", [])
            if not isinstance(values, list):
                continue
            for sample in values:
                if not isinstance(sample, list) or len(sample) != 2:
                    continue
                try:
                    points.append(
                        MetricPoint(
                            timestamp=datetime.fromtimestamp(float(sample[0]), tz=UTC),
                            value=float(sample[1]),
                        )
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
        return MetricSeries(
            service=service,
            metric=metric,
            unit=unit,
            points=tuple(sorted(points, key=lambda p: p.timestamp)),
        )

    async def alerts(self, service: str, start: datetime, end: datetime) -> list[Alert]:
        payload = await self._request("/api/v1/alerts", {})
        data = payload.get("data")
        items = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PrometheusConnectionError("Prometheus вернул некорректный список alerts.")
        found: list[Alert] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            labels = item.get("labels")
            if not isinstance(labels, dict) or labels.get(self.service_label) != service:
                continue
            try:
                fired_at = datetime.fromisoformat(str(item["activeAt"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if not start <= fired_at <= end:
                continue
            severity_value = str(labels.get("severity", "warning")).lower()
            severity = (
                AlertSeverity.CRITICAL
                if severity_value == "critical"
                else AlertSeverity.INFO
                if severity_value == "info"
                else AlertSeverity.WARNING
            )
            annotations = item.get("annotations")
            description = ""
            if isinstance(annotations, dict):
                description = str(
                    annotations.get("description") or annotations.get("summary") or ""
                )
            found.append(
                Alert(
                    name=str(labels.get("alertname", "Prometheus alert")),
                    service=service,
                    severity=severity,
                    fired_at=fired_at.astimezone(UTC),
                    description=description[:2_000],
                )
            )
        return sorted(found, key=lambda alert: alert.fired_at, reverse=True)

    async def check(self) -> None:
        await self._request("/-/ready", {}, expect_json=False)

    async def _request(
        self, path: str, params: dict[str, str], *, expect_json: bool = True
    ) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            if not expect_json:
                return {}
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PrometheusConnectionError(
                "Prometheus недоступен. Проверьте адрес и сеть."
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise PrometheusConnectionError("Prometheus отклонил запрос метрик.")
        return payload


class PrometheusMonitoringProvider:
    """Adapter retaining the graph's typed MonitoringProvider contract."""

    def __init__(self, client: PrometheusClient) -> None:
        self.client = client

    async def get_service_metrics(
        self, service: str, metric: str, start: datetime, end: datetime
    ) -> MetricSeries:
        return await self.client.metric_range(service, metric, start, end)

    async def get_recent_alerts(self, service: str, start: datetime, end: datetime) -> list[Alert]:
        return await self.client.alerts(service, start, end)
