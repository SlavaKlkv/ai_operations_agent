"""Provider protocols separating the agent from concrete data sources.

The graph never talks to Prometheus, GitHub or a log store directly. It talks
to these protocols, which are implemented by mock providers (development,
tests, evaluation) and later by MCP-backed providers.
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
    """Read-only operational runbooks relevant to an investigation."""

    async def search_runbooks(
        self, query: str, service: str | None = None, limit: int = 3
    ) -> list[RunbookHit]: ...


@runtime_checkable
class IssueProvider(Protocol):
    """The issue tracker. The only provider with a write side.

    ``create_issue`` and ``add_issue_comment`` are reachable from the graph
    only through a tool marked :attr:`~app.agent.tools.base.ToolAccess.WRITE`,
    which in turn is reachable only after an approval. The protocol itself
    enforces nothing — that is the point of keeping the policy in one place
    instead of scattering checks through every implementation.
    """

    async def search_issues(
        self, query: str, service: str | None = None, state: str | None = None
    ) -> list[Issue]: ...

    async def get_issue(self, key: str) -> Issue | None: ...

    async def create_issue(self, draft: IssueDraft, *, author: str) -> Issue: ...

    async def add_issue_comment(self, key: str, text: str, *, author: str) -> Issue: ...
