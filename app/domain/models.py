"""Domain models shared by tools, the agent graph and the HTTP API.

Everything the agent observes or produces is a Pydantic model: tool arguments,
tool results and the final analysis. Free-form text is only allowed inside
explicitly textual fields (summaries, descriptions), never as a carrier of
structure.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


# ── Code / deployments ───────────────────────────────────────────────────────


class Deployment(_Frozen):
    """A release of a service to an environment."""

    service: str
    version: str
    environment: str = "production"
    deployed_at: datetime
    commit_sha: str
    deployed_by: str | None = None


class ChangedFile(_Frozen):
    path: str
    additions: int = 0
    deletions: int = 0


class Commit(_Frozen):
    sha: str
    message: str
    author: str
    committed_at: datetime
    files: tuple[ChangedFile, ...] = ()

    @property
    def short_sha(self) -> str:
        return self.sha[:8]


class PullRequest(_Frozen):
    number: int
    title: str
    author: str
    merged_at: datetime | None = None
    commits: tuple[str, ...] = ()
    url: str | None = None


# ── Monitoring ───────────────────────────────────────────────────────────────


class MetricPoint(_Frozen):
    timestamp: datetime
    value: float


class MetricSeries(_Frozen):
    """A single named time series for one service."""

    service: str
    metric: str
    unit: str
    points: tuple[MetricPoint, ...]

    def peak(self) -> MetricPoint | None:
        return max(self.points, key=lambda p: p.value) if self.points else None

    def mean(self) -> float:
        return sum(p.value for p in self.points) / len(self.points) if self.points else 0.0


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Alert(_Frozen):
    name: str
    service: str
    severity: AlertSeverity
    fired_at: datetime
    resolved_at: datetime | None = None
    description: str = ""


# ── Logs ─────────────────────────────────────────────────────────────────────


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogEvent(_Frozen):
    timestamp: datetime
    service: str
    level: LogLevel
    message: str
    error_type: str | None = None
    stack_top: str | None = None


class ErrorGroup(_Frozen):
    """Aggregated log events. Raw logs never reach the model unaggregated."""

    error_type: str
    count: int
    first_seen: datetime
    last_seen: datetime
    sample_message: str
    stack_top: str | None = None
    services: tuple[str, ...] = ()


# ── Analysis ─────────────────────────────────────────────────────────────────


class EvidenceKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    DEPLOYMENT = "deployment"
    COMMIT = "commit"
    ALERT = "alert"
    DOCUMENT = "document"


class Evidence(_Frozen):
    """A single grounded fact the analysis is allowed to rely on.

    ``source_tool`` and ``reference`` exist so that every claim in the final
    report can be traced back to the tool call that produced it.
    """

    kind: EvidenceKind
    summary: str
    source_tool: str
    reference: str
    observed_at: datetime | None = None


class RunbookHit(_Frozen):
    """A bounded, traceable result from the local runbook catalogue."""

    doc_id: str
    title: str
    excerpt: str
    score: float
    services: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


class Hypothesis(_Frozen):
    statement: str
    confidence: Confidence
    supporting_evidence: tuple[str, ...] = ()
    contradicting_evidence: tuple[str, ...] = ()


class IncidentAnalysis(BaseModel):
    """Structured output of the agent — the artefact an issue is built from."""

    model_config = ConfigDict(extra="forbid")

    service: str
    incident_start: datetime | None = None
    symptoms: list[str] = Field(default_factory=list)
    suspected_causes: list[Hypothesis] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = 0.0
    recommended_actions: list[str] = Field(default_factory=list)
    requires_human_review: bool = True
    summary: str = ""


# ── Issues ───────────────────────────────────────────────────────────────────


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Issue(_Frozen):
    """An issue as the tracker reports it, after a read or a write."""

    key: str
    title: str
    body: str = ""
    service: str | None = None
    labels: tuple[str, ...] = ()
    state: IssueState = IssueState.OPEN
    created_at: datetime | None = None
    created_by: str = ""
    comments: tuple[str, ...] = ()
    url: str | None = None


class IssueDraft(BaseModel):
    """A proposed issue, before anyone has agreed to create it.

    Separate from :class:`Issue` on purpose: a draft has no key, no author and
    no URL, because it does not exist yet. Sharing one model would make it
    possible to pass an unsaved draft where a created issue is expected.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=8, max_length=200)
    body: str = Field(min_length=20, max_length=20_000)
    service: str | None = None
    labels: list[str] = Field(default_factory=list, max_length=10)
