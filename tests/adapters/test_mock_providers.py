"""Моковый мир — основа для всех остальных тестов, поэтому его собственное
поведение (фильтрация по окну, агрегация, изоляция сервисов) надо закрепить."""

from __future__ import annotations

import pytest

from app.adapters.mock.dataset import DAY, INCIDENT_START, aggregate_logs
from app.adapters.mock.providers import UnknownMetricError
from app.domain.models import LogLevel

WINDOW = (DAY.replace(hour=14), DAY.replace(hour=15))


async def test_metrics_are_clipped_to_the_requested_window(monitoring):
    series = await monitoring.get_service_metrics(
        "billing-service", "error_rate", INCIDENT_START, WINDOW[1]
    )
    assert series.points
    assert all(p.timestamp >= INCIDENT_START for p in series.points)


async def test_unknown_metric_raises_instead_of_returning_empty(monitoring):
    """Отсутствующий ряд — проблема инструментов, а не наблюдение "нет ошибок"."""
    with pytest.raises(UnknownMetricError):
        await monitoring.get_service_metrics("billing-service", "cpu", *WINDOW)


async def test_deployments_are_service_scoped_and_newest_first(code):
    found = await code.get_recent_deployments("billing-service", DAY, WINDOW[1])
    assert [d.version for d in found] == ["v1.8.4", "v1.8.3"]


async def test_error_groups_are_aggregated_not_raw(logs):
    groups = await logs.get_error_groups("billing-service", *WINDOW)
    assert [g.error_type for g in groups] == ["TypeError", "GatewayTimeout"]
    top = groups[0]
    assert top.count > 1
    assert top.first_seen == INCIDENT_START
    assert top.services == ("billing-service",)


async def test_min_count_filters_rare_groups(logs):
    groups = await logs.get_error_groups("billing-service", *WINDOW, min_count=50)
    assert [g.error_type for g in groups] == ["TypeError"]


def test_aggregation_ignores_non_error_levels(scenario):
    from app.domain.models import LogEvent

    noisy = [
        LogEvent(
            timestamp=INCIDENT_START,
            service="billing-service",
            level=LogLevel.INFO,
            message="ok",
            error_type="NotAnError",
        )
    ]
    groups = aggregate_logs(noisy + scenario.logs, *WINDOW)
    assert "NotAnError" not in {g.error_type for g in groups}


async def test_pull_request_lookup(code):
    assert (await code.get_pull_request("billing-service", 482)).number == 482
    assert await code.get_pull_request("billing-service", 999) is None


async def test_decoy_service_stays_healthy(monitoring):
    """Релиз search-service — отвлекающий фактор: его метрики не двигаются."""
    series = await monitoring.get_service_metrics("search-service", "error_rate", *WINDOW)
    assert max(p.value for p in series.points) < 0.01
