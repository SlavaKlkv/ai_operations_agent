"""A deterministic synthetic environment used for development and evaluation.

The dataset encodes one realistic incident: ``billing-service`` v1.8.4 is
deployed at 14:27 UTC and five minutes later the 5xx rate jumps from a ~0.4 %
baseline to double digits. A decoy deployment of an unrelated service and a
decoy commit exist so that a correct agent has to *correlate* rather than pick
the most recent change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.domain.models import (
    Alert,
    AlertSeverity,
    ChangedFile,
    Commit,
    Deployment,
    ErrorGroup,
    LogEvent,
    LogLevel,
    MetricPoint,
    MetricSeries,
    PullRequest,
)

DAY = datetime(2026, 3, 17, tzinfo=UTC)
INCIDENT_START = DAY.replace(hour=14, minute=32)
DEPLOY_AT = DAY.replace(hour=14, minute=27)


def _at(hour: int, minute: int) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


@dataclass(frozen=True)
class Scenario:
    """Everything the synthetic world knows about one incident."""

    name: str
    deployments: list[Deployment]
    commits: list[Commit]
    pull_requests: list[PullRequest]
    alerts: list[Alert]
    logs: list[LogEvent]
    metrics: dict[tuple[str, str], MetricSeries] = field(default_factory=dict)


def _series(
    service: str,
    metric: str,
    unit: str,
    *,
    baseline: float,
    spike: float,
    spike_from: datetime,
    step: timedelta = timedelta(minutes=1),
    window: tuple[datetime, datetime] = (_at(14, 0), _at(15, 0)),
) -> MetricSeries:
    """Build a flat baseline that steps up to ``spike`` at ``spike_from``."""
    start, end = window
    points: list[MetricPoint] = []
    cursor = start
    while cursor <= end:
        value = spike if cursor >= spike_from else baseline
        # Детерминированное колебание не даёт ряду выглядеть ступенчатой функцией.
        wobble = 1.0 + ((cursor.minute % 5) - 2) * 0.01
        points.append(MetricPoint(timestamp=cursor, value=round(value * wobble, 4)))
        cursor += step
    return MetricSeries(service=service, metric=metric, unit=unit, points=tuple(points))


def _error_logs() -> list[LogEvent]:
    logs: list[LogEvent] = []
    cursor = INCIDENT_START
    while cursor <= _at(15, 0):
        for _ in range(6):
            logs.append(
                LogEvent(
                    timestamp=cursor,
                    service="billing-service",
                    level=LogLevel.ERROR,
                    message="Unhandled error while charging invoice",
                    error_type="TypeError",
                    stack_top="billing/charge.py:184 in apply_tax_rate",
                )
            )
        logs.append(
            LogEvent(
                timestamp=cursor,
                service="billing-service",
                level=LogLevel.ERROR,
                message="Upstream payment gateway timed out",
                error_type="GatewayTimeout",
                stack_top="billing/gateway.py:57 in charge",
            )
        )
        cursor += timedelta(minutes=2)
    return logs


BILLING_5XX = Scenario(
    name="billing-5xx-after-release",
    deployments=[
        Deployment(
            service="billing-service",
            version="v1.8.4",
            deployed_at=DEPLOY_AT,
            commit_sha="9f2c41ab77d3e5b0c18a4fd6e2b9c3157ad0e841",
            deployed_by="ci-bot",
        ),
        Deployment(
            service="billing-service",
            version="v1.8.3",
            deployed_at=_at(9, 12),
            commit_sha="41bd9e0a2c5f8871ba3d0cc7e9f421d8ab6730ee",
            deployed_by="ci-bot",
        ),
        # Ложный след: другой сервис был выпущен ближе по времени к всплеску.
        Deployment(
            service="search-service",
            version="v3.1.0",
            deployed_at=_at(14, 30),
            commit_sha="7c3a5e91bb2d4408ffe6c1027d5a39b84ee20cd1",
            deployed_by="ci-bot",
        ),
    ],
    commits=[
        Commit(
            sha="9f2c41ab77d3e5b0c18a4fd6e2b9c3157ad0e841",
            message="feat(tax): apply regional tax rates to invoice totals",
            author="a.petrova",
            committed_at=_at(13, 41),
            files=(
                ChangedFile(path="billing/charge.py", additions=64, deletions=9),
                ChangedFile(path="billing/tax/rates.py", additions=120, deletions=0),
            ),
        ),
        Commit(
            sha="2ba7f0c18d94e6a35107fbc2d8e04963ac7512bf",
            message="chore: bump structlog to 24.4",
            author="d.ivanov",
            committed_at=_at(12, 5),
            files=(ChangedFile(path="pyproject.toml", additions=1, deletions=1),),
        ),
        Commit(
            sha="41bd9e0a2c5f8871ba3d0cc7e9f421d8ab6730ee",
            message="fix(api): return 404 instead of 500 for unknown invoice",
            author="a.petrova",
            committed_at=_at(8, 55),
            files=(ChangedFile(path="billing/api/invoices.py", additions=7, deletions=3),),
        ),
    ],
    pull_requests=[
        PullRequest(
            number=482,
            title="Regional tax rates for invoice totals",
            author="a.petrova",
            merged_at=_at(14, 2),
            commits=("9f2c41ab77d3e5b0c18a4fd6e2b9c3157ad0e841",),
            url="https://github.com/acme/billing-service/pull/482",
        ),
    ],
    alerts=[
        Alert(
            name="HighErrorRate",
            service="billing-service",
            severity=AlertSeverity.CRITICAL,
            fired_at=_at(14, 35),
            description="5xx ratio above 5% for 3 minutes",
        ),
        Alert(
            name="LatencyP99Degraded",
            service="billing-service",
            severity=AlertSeverity.WARNING,
            fired_at=_at(14, 38),
            description="p99 latency above 1.5s",
        ),
    ],
    logs=_error_logs(),
    metrics={
        ("billing-service", "error_rate"): _series(
            "billing-service",
            "error_rate",
            "ratio",
            baseline=0.004,
            spike=0.113,
            spike_from=INCIDENT_START,
        ),
        ("billing-service", "request_rate"): _series(
            "billing-service",
            "request_rate",
            "rps",
            baseline=240.0,
            spike=236.0,
            spike_from=INCIDENT_START,
        ),
        ("billing-service", "latency_p99"): _series(
            "billing-service",
            "latency_p99",
            "seconds",
            baseline=0.42,
            spike=1.71,
            spike_from=INCIDENT_START,
        ),
        ("billing-service", "latency_p50"): _series(
            "billing-service",
            "latency_p50",
            "seconds",
            baseline=0.08,
            spike=0.11,
            spike_from=INCIDENT_START,
        ),
        ("search-service", "error_rate"): _series(
            "search-service",
            "error_rate",
            "ratio",
            baseline=0.002,
            spike=0.002,
            spike_from=INCIDENT_START,
        ),
    },
)

SCENARIOS: dict[str, Scenario] = {BILLING_5XX.name: BILLING_5XX}
DEFAULT_SCENARIO = BILLING_5XX


def aggregate_logs(
    logs: list[LogEvent], start: datetime, end: datetime, min_count: int = 1
) -> list[ErrorGroup]:
    """Collapse raw log events into per-error-type groups.

    This is the preprocessing layer that keeps unbounded log volume away from
    the model: the agent sees counts and one sample, never the full stream.
    """
    buckets: dict[str, list[LogEvent]] = {}
    for event in logs:
        if not (start <= event.timestamp <= end):
            continue
        if event.level not in (LogLevel.ERROR, LogLevel.CRITICAL):
            continue
        buckets.setdefault(event.error_type or "Unknown", []).append(event)

    groups = [
        ErrorGroup(
            error_type=error_type,
            count=len(events),
            first_seen=min(e.timestamp for e in events),
            last_seen=max(e.timestamp for e in events),
            sample_message=events[0].message,
            stack_top=events[0].stack_top,
            services=tuple(sorted({e.service for e in events})),
        )
        for error_type, events in buckets.items()
        if len(events) >= min_count
    ]
    return sorted(groups, key=lambda g: g.count, reverse=True)


# ── Второй инцидент: зависимость, а не релиз ─────────────────────────────────


def _gateway_logs() -> list[LogEvent]:
    logs: list[LogEvent] = []
    cursor = _at(14, 20)
    while cursor <= _at(15, 0):
        for _ in range(4):
            logs.append(
                LogEvent(
                    timestamp=cursor,
                    service="checkout-service",
                    level=LogLevel.ERROR,
                    message="Upstream inventory service did not respond in time",
                    error_type="GatewayTimeout",
                    stack_top="checkout/inventory_client.py:88 in reserve",
                )
            )
        cursor += timedelta(minutes=2)
    return logs


#: Ошибки растут без близкого по времени деплоя, задержка растёт вместе с ними,
#: а частота запросов остаётся прежней. Корректный агент сообщает, что релизы
#: этого не объясняют; шаблонное обвинение последнего деплоя здесь ошибочно.
CHECKOUT_DEPENDENCY = Scenario(
    name="checkout-dependency-degradation",
    deployments=[
        Deployment(
            service="checkout-service",
            version="v4.2.0",
            deployed_at=_at(6, 5),  # за восемь часов до инцидента
            commit_sha="b1d0f7c93ea4526d8c0f1a7b45e9d2c86f3a01bb",
            deployed_by="ci-bot",
        ),
    ],
    commits=[
        Commit(
            sha="b1d0f7c93ea4526d8c0f1a7b45e9d2c86f3a01bb",
            message="chore(deps): bump http client to 2.9.1",
            author="m.sokolov",
            committed_at=_at(5, 40),
            files=(ChangedFile(path="pyproject.toml", additions=1, deletions=1),),
        ),
    ],
    pull_requests=[],
    alerts=[
        Alert(
            name="HighErrorRate",
            service="checkout-service",
            severity=AlertSeverity.CRITICAL,
            fired_at=_at(14, 26),
            description="5xx ratio above 5% for 3 minutes",
        ),
    ],
    logs=_gateway_logs(),
    metrics={
        ("checkout-service", "error_rate"): _series(
            "checkout-service",
            "error_rate",
            "ratio",
            baseline=0.003,
            spike=0.082,
            spike_from=_at(14, 20),
        ),
        ("checkout-service", "request_rate"): _series(
            "checkout-service",
            "request_rate",
            "rps",
            baseline=180.0,
            spike=178.0,
            spike_from=_at(14, 20),
        ),
        ("checkout-service", "latency_p99"): _series(
            "checkout-service",
            "latency_p99",
            "seconds",
            baseline=0.35,
            spike=2.90,
            spike_from=_at(14, 20),
        ),
    },
)


# ── Третий случай: всё в порядке ─────────────────────────────────────────────

#: Исправный сервис. Агент всё равно проводит расследование, как дежурный инженер
#: при ложном сообщении. Правильный ответ — «ничего не найдено», а не выдуманная
#: правдоподобная причина.
SEARCH_HEALTHY = Scenario(
    name="search-service-no-incident",
    deployments=[
        Deployment(
            service="search-service",
            version="v3.1.0",
            deployed_at=_at(14, 30),
            commit_sha="7c3a5e91bb2d4408ffe6c1027d5a39b84ee20cd1",
            deployed_by="ci-bot",
        ),
    ],
    commits=[
        Commit(
            sha="7c3a5e91bb2d4408ffe6c1027d5a39b84ee20cd1",
            message="feat(search): add fuzzy matching for product names",
            author="k.orlova",
            committed_at=_at(13, 55),
            files=(ChangedFile(path="search/query.py", additions=48, deletions=6),),
        ),
    ],
    pull_requests=[],
    alerts=[],
    logs=[],
    metrics={
        ("search-service", "error_rate"): _series(
            "search-service",
            "error_rate",
            "ratio",
            baseline=0.002,
            spike=0.002,
            spike_from=_at(14, 30),
        ),
        ("search-service", "request_rate"): _series(
            "search-service",
            "request_rate",
            "rps",
            baseline=95.0,
            spike=96.0,
            spike_from=_at(14, 30),
        ),
        ("search-service", "latency_p99"): _series(
            "search-service",
            "latency_p99",
            "seconds",
            baseline=0.21,
            spike=0.22,
            spike_from=_at(14, 30),
        ),
    },
)

SCENARIOS = {s.name: s for s in (BILLING_5XX, CHECKOUT_DEPENDENCY, SEARCH_HEALTHY)}
