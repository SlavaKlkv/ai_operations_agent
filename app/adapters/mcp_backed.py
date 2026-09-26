"""Provider implementations that reach their data over MCP.

This is where the architectural choice of V3 lives. MCP is an *integration
layer*, not a replacement for the agent's tool contract. The agent keeps its
own typed registry, its own guardrails and its own bounded renderings; what
changes is where the data comes from.

Concretely: these classes implement exactly the protocols in
``app.adapters.base`` that the mock providers implement, so
``build_registry(monitoring, code, logs)`` is unchanged, every tool keeps its
Pydantic schema, and swapping mock for MCP is a wiring decision. Had the
agent's tools been generated from whatever the servers advertise, a server
could have widened the agent's reach by editing its own manifest.

Everything crossing the boundary is re-validated. A remote server's response
is untrusted input: it gets parsed into a domain model here, and a response
that does not fit is a failure at the boundary rather than a strange value
appearing three layers later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError

from app.domain.models import (
    Alert,
    AlertSeverity,
    ChangedFile,
    Commit,
    Deployment,
    ErrorGroup,
    Issue,
    IssueDraft,
    IssueState,
    MetricPoint,
    MetricSeries,
    PullRequest,
)
from app.mcp.client import MCPToolPool, ToolCallFailed

log = structlog.get_logger(__name__)

#: Единицы измерения каждой метрики. Серверы возвращают значения без единиц;
#: явное описание здесь сохраняет корректность отображения без изменения протокола.
METRIC_UNITS = {
    "error_rate": "ratio",
    "request_rate": "rps",
    "latency_p50": "seconds",
    "latency_p95": "seconds",
    "latency_p99": "seconds",
}


class RemoteDataError(RuntimeError):
    """A server answered, but not with something this system can use."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _unwrap(payload: dict[str, Any]) -> Any:
    """MCP wraps a non-object return value in ``{"result": ...}``.

    A list-returning tool comes back wrapped; a model-returning tool does not.
    Normalising here keeps that protocol detail out of every call site.
    """
    if set(payload) == {"result"}:
        return payload["result"]
    return payload


class MCPMonitoringProvider:
    """Metrics, alerts and aggregated errors, over the monitoring server."""

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def get_service_metrics(
        self, service: str, metric: str, start: datetime, end: datetime
    ) -> MetricSeries:
        payload = _unwrap(
            await self._pool.call(
                "get_service_metrics",
                {"service": service, "metric": metric, "start": _iso(start), "end": _iso(end)},
            )
        )
        try:
            points = tuple(
                MetricPoint(timestamp=_at(p["at"]), value=float(p["value"]))
                for p in payload.get("points", [])
            )
            return MetricSeries(
                service=payload["service"],
                metric=payload["metric"],
                unit=payload.get("unit") or METRIC_UNITS.get(metric, ""),
                points=points,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed metrics for {service}/{metric}: {exc}") from exc

    async def get_recent_alerts(self, service: str, start: datetime, end: datetime) -> list[Alert]:
        payload = _unwrap(
            await self._pool.call(
                "get_recent_alerts",
                {"service": service, "start": _iso(start), "end": _iso(end)},
            )
        )
        try:
            return [
                Alert(
                    name=a["name"],
                    service=a["service"],
                    severity=AlertSeverity(a["severity"]),
                    fired_at=_at(a["fired_at"]),
                    resolved_at=_at(a["resolved_at"]) if a.get("resolved_at") else None,
                    description=a.get("description", ""),
                )
                for a in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed alerts for {service}: {exc}") from exc


class MCPLogProvider:
    """Aggregated application errors, over the monitoring server.

    A separate provider from monitoring even though one server backs both:
    the agent's log tool and metric tool are independent capabilities, and
    tying them to one class would make it impossible to point them at
    different backends later — which is the normal end state.
    """

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def get_error_groups(
        self, service: str, start: datetime, end: datetime, min_count: int = 1
    ) -> list[ErrorGroup]:
        payload = _unwrap(
            await self._pool.call(
                "get_error_groups",
                {
                    "service": service,
                    "start": _iso(start),
                    "end": _iso(end),
                    "min_count": min_count,
                },
            )
        )
        try:
            return [
                ErrorGroup(
                    error_type=g["error_type"],
                    count=int(g["count"]),
                    first_seen=_at(g["first_seen"]),
                    last_seen=_at(g["last_seen"]),
                    sample_message=g.get("sample_message", ""),
                    stack_top=g.get("stack_top"),
                    services=tuple(g.get("services", ())),
                )
                for g in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed error groups for {service}: {exc}") from exc


class MCPCodeProvider:
    """Deployments, commits and pull requests, over the code server."""

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def get_recent_deployments(
        self, service: str, start: datetime, end: datetime
    ) -> list[Deployment]:
        payload = _unwrap(
            await self._pool.call(
                "get_recent_deployments",
                {"service": service, "start": _iso(start), "end": _iso(end)},
            )
        )
        try:
            return [
                Deployment(
                    service=d["service"],
                    version=d["version"],
                    environment=d.get("environment", "production"),
                    deployed_at=_at(d["deployed_at"]),
                    commit_sha=d["commit_sha"],
                    deployed_by=d.get("deployed_by"),
                )
                for d in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed deployments for {service}: {exc}") from exc

    async def get_commits(self, service: str, since: datetime, until: datetime) -> list[Commit]:
        payload = _unwrap(
            await self._pool.call(
                "get_commits",
                {"service": service, "start": _iso(since), "end": _iso(until)},
            )
        )
        try:
            return [
                Commit(
                    sha=c["sha"],
                    message=c["message"],
                    author=c["author"],
                    committed_at=_at(c["committed_at"]),
                    files=tuple(
                        ChangedFile(
                            path=f["path"],
                            additions=int(f.get("additions", 0)),
                            deletions=int(f.get("deletions", 0)),
                        )
                        for f in c.get("files", ())
                    ),
                )
                for c in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed commits for {service}: {exc}") from exc

    async def get_pull_request(self, service: str, number: int) -> PullRequest | None:
        """A missing pull request is an answer, not a failure.

        The server reports "no such PR" as a tool error because that is what
        it is at the protocol level; at the domain level it is simply ``None``,
        and the agent should not have to distinguish it from a server outage.
        """
        try:
            payload = _unwrap(
                await self._pool.call("get_pull_request", {"service": service, "number": number})
            )
        except ToolCallFailed as exc:
            log.info("mcp.pull_request_missing", number=number, detail=str(exc))
            return None
        try:
            return PullRequest(
                number=int(payload["number"]),
                title=payload["title"],
                author=payload["author"],
                merged_at=_at(payload["merged_at"]) if payload.get("merged_at") else None,
                commits=tuple(payload.get("commits", ())),
                url=payload.get("url"),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed pull request #{number}: {exc}") from exc


class MCPKnowledgeProvider:
    """Runbook retrieval, over the knowledge server.

    Not part of :mod:`app.adapters.base`: knowledge is a capability the agent
    gained with MCP rather than one the deterministic workflow already had, so
    it gets its own protocol instead of being retrofitted into an existing one.
    """

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def search_runbooks(
        self, query: str, service: str | None = None, limit: int = 3
    ) -> list[dict[str, Any]]:
        payload = _unwrap(
            await self._pool.call(
                "search_runbooks", {"query": query, "service": service, "limit": limit}
            )
        )
        if not isinstance(payload, list):
            raise RemoteDataError("runbook search did not return a list of hits")
        return payload


class MCPIssueProvider:
    """The issue tracker, over the incident server — reads and writes.

    The ``approved`` flag is threaded through to the pool rather than stored
    on this object. A provider that could be constructed "in write mode"
    would carry that permission into every later call; passing it per call
    keeps an approval attached to the single action it was given for.
    """

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def search_issues(
        self, query: str, service: str | None = None, state: str | None = None
    ) -> list[Issue]:
        payload = _unwrap(
            await self._pool.call(
                "search_issues", {"query": query, "service": service, "state": state}
            )
        )
        return [_issue(item) for item in payload]

    async def get_issue(self, key: str) -> Issue | None:
        try:
            return _issue(_unwrap(await self._pool.call("get_issue", {"key": key})))
        except ToolCallFailed:
            return None

    async def create_issue(self, draft: IssueDraft, *, author: str) -> Issue:
        payload = _unwrap(
            await self._pool.call(
                "create_issue",
                {
                    "title": draft.title,
                    "body": draft.body,
                    "service": draft.service,
                    "labels": draft.labels,
                    "author": author,
                },
                approved=True,
            )
        )
        return _issue(payload)

    async def add_issue_comment(self, key: str, text: str, *, author: str) -> Issue:
        payload = _unwrap(
            await self._pool.call(
                "add_issue_comment",
                {"key": key, "text": text, "author": author},
                approved=True,
            )
        )
        return _issue(payload)


def _issue(payload: dict[str, Any]) -> Issue:
    try:
        return Issue(
            key=payload["key"],
            title=payload["title"],
            body=payload.get("body", ""),
            service=payload.get("service"),
            labels=tuple(payload.get("labels", ())),
            state=IssueState(payload.get("state", "open")),
            created_at=_at(payload["created_at"]) if payload.get("created_at") else None,
            created_by=payload.get("created_by", ""),
            comments=tuple(payload.get("comments", ())),
            url=payload.get("url"),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RemoteDataError(f"malformed issue payload: {exc}") from exc
