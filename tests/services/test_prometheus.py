"""Граница Prometheus принимает только свой фиксированный контракт метрик только для чтения."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.services.prometheus import PrometheusClient, PrometheusConnectionError


def _client(handler) -> PrometheusClient:
    return PrometheusClient(
        "http://prometheus.local:9090",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_metric_range_uses_fixed_query_and_parses_valid_samples():
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        query = request.url.params["query"]
        assert "http_requests_total" in query
        assert 'service="billing-service"' in query
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {"values": [[now.timestamp(), "0.1"], [now.timestamp() + 60, "0.25"]]}
                    ],
                },
            },
        )

    client = _client(handler)
    series = await client.metric_range(
        "billing-service", "error_rate", now - timedelta(minutes=5), now
    )
    await client.close()

    assert series.unit == "ratio"
    assert [point.value for point in series.points] == [0.1, 0.25]


async def test_metric_range_escapes_label_value_and_rejects_unknown_metric():
    observed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.url.params["query"])
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    client = _client(handler)
    now = datetime.now(UTC)
    await client.metric_range('api"} or up{', "request_rate", now - timedelta(minutes=1), now)
    with pytest.raises(PrometheusConnectionError, match="не поддерживается"):
        await client.metric_range("api", "arbitrary_promql", now - timedelta(minutes=1), now)
    await client.close()

    assert 'service="api\\"} or up{"' in observed[0]


async def test_alerts_filter_by_service_and_normalize_severity():
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/alerts"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "alerts": [
                        {
                            "activeAt": now.isoformat(),
                            "labels": {
                                "alertname": "High5xx",
                                "service": "billing-service",
                                "severity": "critical",
                            },
                            "annotations": {"summary": "Too many errors"},
                        },
                        {
                            "activeAt": now.isoformat(),
                            "labels": {"alertname": "Other", "service": "search-service"},
                        },
                    ]
                },
            },
        )

    client = _client(handler)
    alerts = await client.alerts(
        "billing-service", now - timedelta(minutes=1), now + timedelta(minutes=1)
    )
    await client.close()

    assert [(item.name, item.severity, item.description) for item in alerts] == [
        ("High5xx", "critical", "Too many errors")
    ]


@pytest.mark.parametrize(
    "url",
    ["prometheus.local", "ftp://prometheus.local", "http://user:secret@prometheus.local"],
)
def test_prometheus_rejects_unsafe_or_incomplete_url(url):
    with pytest.raises(PrometheusConnectionError):
        PrometheusClient(url)
