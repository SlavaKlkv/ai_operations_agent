"""Argument and result schemas for every tool.

Two rules shape these models.

*Arguments* are as small as they can be. The time window is optional on every
query tool: if the model omits it, the executor injects the window the task
analysis resolved. That is deliberate — a model that has to invent timestamps
will invent them, and an investigation anchored to a hallucinated window is
worse than one anchored to a slightly wrong default.

*Results* are typed domain objects, not prose. The graph keeps them; the model
sees only what the tool's ``render`` function produces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import (
    Alert,
    Commit,
    Deployment,
    ErrorGroup,
    Issue,
    MetricSeries,
    PullRequest,
    RunbookHit,
)

#: Метрики, которые гарантированно предоставляет слой мониторинга. Ограничение
#: типом Literal заставляет выдуманное имя метрики упасть на валидации, а не
#: не даёт неверному имени дойти до провайдера и вернуться неясной ошибкой поиска.
MetricName = Literal["error_rate", "request_rate", "latency_p50", "latency_p95", "latency_p99"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WindowArgs(_Args):
    """Shared optional window. Omit both to use the investigation's window."""

    start: datetime | None = Field(
        default=None, description="Inclusive start (ISO 8601). Omit to use the incident window."
    )
    end: datetime | None = Field(
        default=None, description="Inclusive end (ISO 8601). Omit to use the incident window."
    )


# ── Monitoring ───────────────────────────────────────────────────────────────


class GetServiceMetricsArgs(WindowArgs):
    service: str = Field(description="Service name, e.g. 'billing-service'.")
    metric: MetricName = Field(description="Which time series to fetch.")


class MetricsResult(_Result):
    series: MetricSeries


class GetRecentAlertsArgs(WindowArgs):
    service: str = Field(description="Service whose alerts to list.")


class AlertsResult(_Result):
    alerts: tuple[Alert, ...] = ()


# ── Код и развёртывания ─────────────────────────────────────────────────────


class GetRecentDeploymentsArgs(WindowArgs):
    service: str = Field(description="Service whose releases to list, newest first.")


class DeploymentsResult(_Result):
    deployments: tuple[Deployment, ...] = ()


class GetCommitsArgs(WindowArgs):
    service: str = Field(description="Service whose repository to read.")


class CommitsResult(_Result):
    commits: tuple[Commit, ...] = ()


class GetPullRequestArgs(_Args):
    service: str = Field(description="Service whose repository holds the pull request.")
    number: int = Field(gt=0, description="Pull request number.")


class PullRequestResult(_Result):
    pull_request: PullRequest | None = None


# ── Logs ─────────────────────────────────────────────────────────────────────


class GetErrorGroupsArgs(WindowArgs):
    service: str = Field(description="Service whose errors to aggregate.")
    min_count: int = Field(
        default=1, ge=1, le=10_000, description="Drop groups with fewer occurrences than this."
    )


class ErrorGroupsResult(_Result):
    groups: tuple[ErrorGroup, ...] = ()


# ── Runbooks ─────────────────────────────────────────────────────────────────


class SearchRunbooksArgs(_Args):
    query: str = Field(
        min_length=1, max_length=400, description="Keywords describing the incident."
    )
    service: str | None = Field(default=None, description="Prefer runbooks for one service.")
    limit: int = Field(default=3, ge=1, le=10, description="Maximum number of relevant runbooks.")


class RunbooksResult(_Result):
    hits: tuple[RunbookHit, ...] = ()


# ── Issues ───────────────────────────────────────────────────────────────────


class SearchIssuesArgs(_Args):
    query: str = Field(
        default="", max_length=400, description="Free text matched against title and body."
    )
    service: str | None = Field(default=None, description="Restrict to one service.")
    state: Literal["open", "closed"] | None = Field(
        default=None, description="Restrict to open or closed issues."
    )


class IssuesResult(_Result):
    issues: tuple[Issue, ...] = ()


class CreateIssueArgs(_Args):
    """Arguments for the one tool that changes an external system.

    The bounds are tighter than the tracker's own, and deliberately so: a
    title short enough to be meaningless or a body long enough to be a log
    dump are both signs the agent has lost the thread, and the right moment
    to catch that is before a human is asked to approve it.
    """

    title: str = Field(min_length=8, max_length=200, description="One line stating what is wrong.")
    body: str = Field(
        min_length=20,
        max_length=20_000,
        description="The analysis: symptoms, evidence, suspected cause, recommended actions.",
    )
    service: str | None = Field(default=None, description="Service the issue is about.")
    labels: list[str] = Field(default_factory=list, max_length=10, description="Labels to apply.")


class AddIssueCommentArgs(_Args):
    key: str = Field(pattern=r"^[A-Z]+-\d+$", description="Issue key, e.g. OPS-12.")
    text: str = Field(min_length=1, max_length=20_000, description="Comment body.")


class IssueResult(_Result):
    issue: Issue
