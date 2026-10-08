"""Bind the provider protocols to concrete, described, renderable tools.

This module is the whole surface the agent has on the outside world. Adding a
capability means adding an entry here; there is no other path from the graph to
a provider, which is what makes the allowlist meaningful.

The ``render`` functions matter as much as the handlers. A metric series is
sixty numbers the graph needs and the model does not: the model gets baseline,
peak and when the change happened, because that is what a conclusion can be
drawn from. Keeping raw volume out of the prompt is a correctness measure, not
only a cost measure.
"""

from __future__ import annotations

from app.adapters.base import (
    CodeProvider,
    IssueProvider,
    KnowledgeProvider,
    LogProvider,
    MonitoringProvider,
)
from app.agent.tools.base import AgentTool, ToolAccess, ToolRegistry
from app.agent.tools.schemas import (
    AddIssueCommentArgs,
    AlertsResult,
    CommitsResult,
    CreateIssueArgs,
    DeploymentsResult,
    ErrorGroupsResult,
    GetCommitsArgs,
    GetErrorGroupsArgs,
    GetPullRequestArgs,
    GetRecentAlertsArgs,
    GetRecentDeploymentsArgs,
    GetServiceMetricsArgs,
    IssueResult,
    IssuesResult,
    MetricsResult,
    PullRequestResult,
    RunbooksResult,
    SearchIssuesArgs,
    SearchRunbooksArgs,
)
from app.domain.models import IssueDraft

#: Сколько элементов результата-списка можно передать модели.
RENDER_LIMIT = 8


def _require_window(args) -> tuple:
    """Windows are injected by the executor; reaching a handler without one is a bug."""
    if args.start is None or args.end is None:
        raise ValueError("time window was not resolved before the tool ran")
    return args.start, args.end


# ── Renderers ────────────────────────────────────────────────────────────────


def _render_metrics(result: MetricsResult) -> str:
    series = result.series
    if not series.points:
        return f"{series.metric} for {series.service}: no samples in the window."
    first, last = series.points[0], series.points[-1]
    peak = series.peak()
    assert peak is not None
    return (
        f"{series.metric} for {series.service} ({series.unit}): "
        f"{len(series.points)} samples from {first.timestamp:%H:%M} to {last.timestamp:%H:%M} UTC; "
        f"first {first.value:g}, last {last.value:g}, mean {series.mean():.4g}, "
        f"peak {peak.value:g} at {peak.timestamp:%H:%M} UTC."
    )


def _render_alerts(result: AlertsResult) -> str:
    if not result.alerts:
        return "No alerts fired in the window."
    lines = [
        f"- {a.name} [{a.severity}] fired {a.fired_at:%H:%M} UTC: {a.description}"
        for a in result.alerts[:RENDER_LIMIT]
    ]
    return _with_overflow(f"{len(result.alerts)} alert(s):", lines, len(result.alerts))


def _render_deployments(result: DeploymentsResult) -> str:
    if not result.deployments:
        return "No deployments in the window."
    lines = [
        f"- {d.service} {d.version} at {d.deployed_at:%H:%M} UTC "
        f"from {d.commit_sha[:8]} by {d.deployed_by or 'unknown'}"
        for d in result.deployments[:RENDER_LIMIT]
    ]
    return _with_overflow(
        f"{len(result.deployments)} deployment(s), newest first:", lines, len(result.deployments)
    )


def _render_commits(result: CommitsResult) -> str:
    if not result.commits:
        return "No commits in the window."
    lines = []
    for c in result.commits[:RENDER_LIMIT]:
        files = ", ".join(f.path for f in c.files[:4]) or "no files recorded"
        churn = sum(f.additions + f.deletions for f in c.files)
        lines.append(
            f"- {c.short_sha} {c.message} — {c.author}, {c.committed_at:%H:%M} UTC, "
            f"{churn} lines across {files}"
        )
    return _with_overflow(
        f"{len(result.commits)} commit(s), newest first:", lines, len(result.commits)
    )


def _render_pull_request(result: PullRequestResult) -> str:
    pr = result.pull_request
    if pr is None:
        return "No such pull request."
    merged = f"merged {pr.merged_at:%H:%M} UTC" if pr.merged_at else "not merged"
    return (
        f"PR #{pr.number} '{pr.title}' by {pr.author}, {merged}, "
        f"{len(pr.commits)} commit(s){f' — {pr.url}' if pr.url else ''}."
    )


def _render_error_groups(result: ErrorGroupsResult) -> str:
    if not result.groups:
        return "No errors in the window."
    lines = [
        f"- {g.count}x {g.error_type} between {g.first_seen:%H:%M} and {g.last_seen:%H:%M} UTC"
        f"{f' at {g.stack_top}' if g.stack_top else ''}: {g.sample_message}"
        for g in result.groups[:RENDER_LIMIT]
    ]
    return _with_overflow(
        f"{len(result.groups)} error group(s), most frequent first:", lines, len(result.groups)
    )


def _render_runbooks(result: RunbooksResult) -> str:
    if not result.hits:
        return "No relevant local runbooks were found."
    lines = [f"- {hit.title} ({hit.doc_id}): {hit.excerpt}" for hit in result.hits[:RENDER_LIMIT]]
    return _with_overflow(f"{len(result.hits)} relevant runbook(s):", lines, len(result.hits))


def _render_issues(result: IssuesResult) -> str:
    if not result.issues:
        return "No matching issues."
    lines = [
        f"- {i.key} [{i.state}] {i.title}" + (f" ({', '.join(i.labels)})" if i.labels else "")
        for i in result.issues[:RENDER_LIMIT]
    ]
    return _with_overflow(f"{len(result.issues)} issue(s):", lines, len(result.issues))


def _render_issue(result: IssueResult) -> str:
    issue = result.issue
    return (
        f"{issue.key} [{issue.state}] {issue.title}"
        f"{f' — {issue.url}' if issue.url else ''}"
        f"{f' ({len(issue.comments)} comment(s))' if issue.comments else ''}"
    )


def _with_overflow(header: str, lines: list[str], total: int) -> str:
    body = "\n".join(lines)
    if total > RENDER_LIMIT:
        body += f"\n- … {total - RENDER_LIMIT} more not shown"
    return f"{header}\n{body}"


# ── Catalogue ────────────────────────────────────────────────────────────────


def build_registry(
    monitoring: MonitoringProvider,
    code: CodeProvider,
    logs: LogProvider,
    issues: IssueProvider | None = None,
    knowledge: KnowledgeProvider | None = None,
    *,
    actor: str = "ai-operations-agent",
) -> ToolRegistry:
    """Every tool the agent has, read and write.

    Registering a write tool does not make it reachable: the guardrails hide
    every :attr:`ToolAccess.WRITE` tool from the planner and refuse to execute
    one unless the run carries an approval. Registration and permission are
    separate on purpose — the catalogue says what exists, the policy says what
    may run, and conflating them is how a tool ends up callable by accident.
    """

    async def get_service_metrics(args: GetServiceMetricsArgs) -> MetricsResult:
        start, end = _require_window(args)
        series = await monitoring.get_service_metrics(args.service, args.metric, start, end)
        return MetricsResult(series=series)

    async def get_recent_alerts(args: GetRecentAlertsArgs) -> AlertsResult:
        start, end = _require_window(args)
        return AlertsResult(
            alerts=tuple(await monitoring.get_recent_alerts(args.service, start, end))
        )

    async def get_recent_deployments(args: GetRecentDeploymentsArgs) -> DeploymentsResult:
        start, end = _require_window(args)
        found = await code.get_recent_deployments(args.service, start, end)
        return DeploymentsResult(deployments=tuple(found))

    async def get_commits(args: GetCommitsArgs) -> CommitsResult:
        start, end = _require_window(args)
        return CommitsResult(commits=tuple(await code.get_commits(args.service, start, end)))

    async def get_pull_request(args: GetPullRequestArgs) -> PullRequestResult:
        return PullRequestResult(
            pull_request=await code.get_pull_request(args.service, args.number)
        )

    async def get_error_groups(args: GetErrorGroupsArgs) -> ErrorGroupsResult:
        start, end = _require_window(args)
        found = await logs.get_error_groups(args.service, start, end, args.min_count)
        return ErrorGroupsResult(groups=tuple(found))

    async def search_runbooks(args: SearchRunbooksArgs) -> RunbooksResult:
        assert knowledge is not None
        return RunbooksResult(
            hits=tuple(await knowledge.search_runbooks(args.query, args.service, args.limit))
        )

    async def search_issues(args: SearchIssuesArgs) -> IssuesResult:
        assert issues is not None
        found = await issues.search_issues(args.query, args.service, args.state)
        return IssuesResult(issues=tuple(found))

    async def create_issue(args: CreateIssueArgs) -> IssueResult:
        assert issues is not None
        draft = IssueDraft(
            title=args.title, body=args.body, service=args.service, labels=args.labels
        )
        return IssueResult(issue=await issues.create_issue(draft, author=actor))

    async def add_issue_comment(args: AddIssueCommentArgs) -> IssueResult:
        assert issues is not None
        return IssueResult(issue=await issues.add_issue_comment(args.key, args.text, author=actor))

    tools = [
        AgentTool(
            name="get_service_metrics",
            description=(
                "Time series for one service metric over the incident window. "
                "Returns sample count, first/last/mean values and the peak with its "
                "timestamp — use it to establish when a change in behaviour began."
            ),
            args_schema=GetServiceMetricsArgs,
            result_schema=MetricsResult,
            access=ToolAccess.READ,
            handler=get_service_metrics,
            render=_render_metrics,
        ),
        AgentTool(
            name="get_recent_alerts",
            description=(
                "Alerts that fired for a service in the window, with severity and the "
                "condition that triggered them. Useful for confirming that monitoring "
                "agreed something was wrong, and when."
            ),
            args_schema=GetRecentAlertsArgs,
            result_schema=AlertsResult,
            access=ToolAccess.READ,
            handler=get_recent_alerts,
            render=_render_alerts,
        ),
        AgentTool(
            name="get_recent_deployments",
            description=(
                "Releases of a service in the window, newest first, each with its "
                "timestamp and commit SHA. The primary way to find a change that "
                "precedes an incident."
            ),
            args_schema=GetRecentDeploymentsArgs,
            result_schema=DeploymentsResult,
            access=ToolAccess.READ,
            handler=get_recent_deployments,
            render=_render_deployments,
        ),
        AgentTool(
            name="get_commits",
            description=(
                "Commits in a service repository within the window, newest first, with "
                "author, message and changed files. Use after a suspect deployment is "
                "known, to see what it actually shipped."
            ),
            args_schema=GetCommitsArgs,
            result_schema=CommitsResult,
            access=ToolAccess.READ,
            handler=get_commits,
            render=_render_commits,
            cost=2,
        ),
        AgentTool(
            name="get_pull_request",
            description=(
                "One pull request by number: title, author, merge time and commits. "
                "Use only when a specific PR number is already known."
            ),
            args_schema=GetPullRequestArgs,
            result_schema=PullRequestResult,
            access=ToolAccess.READ,
            handler=get_pull_request,
            render=_render_pull_request,
        ),
        AgentTool(
            name="get_error_groups",
            description=(
                "Application errors in the window, aggregated by error type, with counts, "
                "first/last occurrence, a sample message and the failing stack frame. "
                "Raw log lines are never returned."
            ),
            args_schema=GetErrorGroupsArgs,
            result_schema=ErrorGroupsResult,
            access=ToolAccess.READ,
            handler=get_error_groups,
            render=_render_error_groups,
            cost=2,
        ),
    ]

    if issues is not None:
        tools += [
            AgentTool(
                name="search_issues",
                description=(
                    "Search the issue tracker by free text, optionally by service and "
                    "state. Use it before proposing a new issue: filing a duplicate of "
                    "something already tracked is worse than filing nothing."
                ),
                args_schema=SearchIssuesArgs,
                result_schema=IssuesResult,
                access=ToolAccess.READ,
                handler=search_issues,
                render=_render_issues,
            ),
            AgentTool(
                name="create_issue",
                description=(
                    "Create an issue in the tracker. This changes an external system "
                    "and runs only after a human has approved the exact content."
                ),
                args_schema=CreateIssueArgs,
                result_schema=IssueResult,
                access=ToolAccess.WRITE,
                handler=create_issue,
                render=_render_issue,
                cost=5,
            ),
            AgentTool(
                name="add_issue_comment",
                description=(
                    "Append a comment to an existing issue. This changes an external "
                    "system and runs only after a human has approved it."
                ),
                args_schema=AddIssueCommentArgs,
                result_schema=IssueResult,
                access=ToolAccess.WRITE,
                handler=add_issue_comment,
                render=_render_issue,
                cost=3,
            ),
        ]

    if knowledge is not None:
        tools.append(
            AgentTool(
                name="search_runbooks",
                description=(
                    "Search the user-owned local Markdown runbook catalogue for documented "
                    "mitigation relevant to the observed incident. Read-only."
                ),
                args_schema=SearchRunbooksArgs,
                result_schema=RunbooksResult,
                access=ToolAccess.READ,
                handler=search_runbooks,
                render=_render_runbooks,
            )
        )

    return ToolRegistry(tools)
