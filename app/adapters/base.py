"""Протоколы провайдеров, отделяющие агента от конкретных источников данных.

Граф никогда не обращается напрямую к Prometheus, GitHub или хранилищу логов.
Он обращается к этим протоколам, которые реализуются mock-провайдерами
(разработка, тесты, оценка), а позднее — провайдерами на базе MCP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.models import (
    Alert,
    Commit,
    Deployment,
    ErrorGroup,
    Issue,
    IssueDraft,
    MetricSeries,
    PullRequest,
    RunbookHit,
)


@runtime_checkable
class MonitoringProvider(Protocol):
    async def get_service_metrics(
        self, service: str, metric: str, start: datetime, end: datetime
    ) -> MetricSeries: ...

    async def get_recent_alerts(
        self, service: str, start: datetime, end: datetime
    ) -> list[Alert]: ...


@runtime_checkable
class CodeProvider(Protocol):
    async def get_recent_deployments(
        self, service: str, start: datetime, end: datetime
    ) -> list[Deployment]: ...

    async def get_commits(self, service: str, since: datetime, until: datetime) -> list[Commit]: ...

    async def get_pull_request(self, service: str, number: int) -> PullRequest | None: ...


@runtime_checkable
class LogProvider(Protocol):
    async def get_error_groups(
        self, service: str, start: datetime, end: datetime, min_count: int = 1
    ) -> list[ErrorGroup]: ...


@runtime_checkable
class KnowledgeProvider(Protocol):
    """Ранбуки по эксплуатации только для чтения, относящиеся к расследованию."""

    async def search_runbooks(
        self, query: str, service: str | None = None, limit: int = 3
    ) -> list[RunbookHit]: ...


@runtime_checkable
class IssueProvider(Protocol):
    """Трекер задач. Единственный провайдер со стороной записи.

    create_issue и add_issue_comment достижимы из графа только через
    инструмент с пометкой ToolAccess.WRITE,
    который, в свою очередь, достижим только после подтверждения. Сам протокол
    ничего не обеспечивает — в этом и смысл держать политику в одном месте,
    а не разбрасывать проверки по всем реализациям.
    """

    async def search_issues(
        self, query: str, service: str | None = None, state: str | None = None
    ) -> list[Issue]: ...

    async def get_issue(self, key: str) -> Issue | None: ...

    async def create_issue(self, draft: IssueDraft, *, author: str) -> Issue: ...

    async def add_issue_comment(self, key: str, text: str, *, author: str) -> Issue: ...
