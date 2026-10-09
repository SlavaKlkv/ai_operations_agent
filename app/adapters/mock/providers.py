"""Mock-реализации протоколов провайдеров на основе Scenario."""

from __future__ import annotations

from datetime import UTC, datetime

from app.adapters.mock.dataset import DEFAULT_SCENARIO, Scenario, aggregate_logs
from app.domain.models import (
    Alert,
    Commit,
    Deployment,
    ErrorGroup,
    Issue,
    IssueDraft,
    IssueState,
    MetricSeries,
    PullRequest,
)


class UnknownMetricError(LookupError):
    """Возникает, когда в сценарии нет ряда для запрошенных сервиса и метрики."""


class UnknownIssueError(LookupError):
    """Возникает, когда комментарий адресован несуществующему issue."""


class DuplicateIssueError(ValueError):
    """Возникает, когда новый issue повторяет заголовок открытого.

    Создание дубликата — характерный сбой автоматизированного репортёра
    инцидентов, поэтому оно отклоняется на провайдере, а не оставляется на
    усмотрение вызывающей стороны.
    """


class MockMonitoringProvider:
    def __init__(self, scenario: Scenario = DEFAULT_SCENARIO) -> None:
        self._scenario = scenario

    async def get_service_metrics(
        self, service: str, metric: str, start: datetime, end: datetime
    ) -> MetricSeries:
        series = self._scenario.metrics.get((service, metric))
        if series is None:
            raise UnknownMetricError(f"no series {metric!r} for service {service!r}")
        points = tuple(p for p in series.points if start <= p.timestamp <= end)
        return series.model_copy(update={"points": points})

    async def get_recent_alerts(self, service: str, start: datetime, end: datetime) -> list[Alert]:
        return [
            a for a in self._scenario.alerts if a.service == service and start <= a.fired_at <= end
        ]


class MockCodeProvider:
    def __init__(self, scenario: Scenario = DEFAULT_SCENARIO) -> None:
        self._scenario = scenario

    async def get_recent_deployments(
        self, service: str, start: datetime, end: datetime
    ) -> list[Deployment]:
        found = [
            d
            for d in self._scenario.deployments
            if d.service == service and start <= d.deployed_at <= end
        ]
        return sorted(found, key=lambda d: d.deployed_at, reverse=True)

    async def get_commits(self, service: str, since: datetime, until: datetime) -> list[Commit]:
        del service  # в синтетическом мире каждому сценарию соответствует один репозиторий
        found = [c for c in self._scenario.commits if since <= c.committed_at <= until]
        return sorted(found, key=lambda c: c.committed_at, reverse=True)

    async def get_pull_request(self, service: str, number: int) -> PullRequest | None:
        del service
        return next((pr for pr in self._scenario.pull_requests if pr.number == number), None)


class MockLogProvider:
    def __init__(self, scenario: Scenario = DEFAULT_SCENARIO) -> None:
        self._scenario = scenario

    async def get_error_groups(
        self, service: str, start: datetime, end: datetime, min_count: int = 1
    ) -> list[ErrorGroup]:
        logs = [e for e in self._scenario.logs if e.service == service]
        return aggregate_logs(logs, start, end, min_count)


class MockIssueProvider:
    """Трекер задач в памяти для разработки, тестов и оценки.

    Он фиксирует записи, а не притворяется идемпотентным, так что тест может
    проверить не только то, что агент сказал, будто создаст issue, но и то,
    что был создан ровно один issue с одобренным содержимым.
    """

    def __init__(self, seed: list[Issue] | None = None) -> None:
        self._issues: dict[str, Issue] = {i.key: i for i in seed or _seed_issues()}
        self._counter = len(self._issues)

    @property
    def issues(self) -> list[Issue]:
        return list(self._issues.values())

    async def search_issues(
        self, query: str, service: str | None = None, state: str | None = None
    ) -> list[Issue]:
        needle = query.casefold()
        return [
            issue
            for issue in self._issues.values()
            if (not needle or needle in issue.title.casefold() or needle in issue.body.casefold())
            and (service is None or issue.service == service)
            and (state is None or issue.state == state)
        ]

    async def get_issue(self, key: str) -> Issue | None:
        return self._issues.get(key)

    async def create_issue(self, draft: IssueDraft, *, author: str) -> Issue:
        if any(
            i.title.casefold() == draft.title.casefold() and i.state is IssueState.OPEN
            for i in self._issues.values()
        ):
            raise DuplicateIssueError(f"an open issue titled {draft.title!r} already exists")
        self._counter += 1
        key = f"OPS-{self._counter}"
        issue = Issue(
            key=key,
            title=draft.title,
            body=draft.body,
            service=draft.service,
            labels=tuple(draft.labels),
            created_at=datetime.now(UTC),
            created_by=author,
            url=f"https://issues.example.com/{key}",
        )
        self._issues[key] = issue
        return issue

    async def add_issue_comment(self, key: str, text: str, *, author: str) -> Issue:
        issue = self._issues.get(key)
        if issue is None:
            raise UnknownIssueError(f"no issue {key!r}")
        updated = issue.model_copy(update={"comments": (*issue.comments, f"{author}: {text}")})
        self._issues[key] = updated
        return updated


def _seed_issues() -> list[Issue]:
    """Один заранее существующий issue, чтобы у проверки на дубликаты было что найти."""
    return [
        Issue(
            key="OPS-1",
            title="Intermittent gateway timeouts in billing-service",
            body="Known, low volume, retried by the client.",
            service="billing-service",
            labels=("billing", "known-issue"),
            created_at=datetime(2026, 2, 4, 9, 12, tzinfo=UTC),
            created_by="d.ivanov",
            url="https://issues.example.com/OPS-1",
        )
    ]
