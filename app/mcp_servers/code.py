"""Code MCP server: releases, commits and pull requests.

Stands in for a VCS and a deployment system. The two are one server because
the question the agent actually asks — "what shipped, and when" — spans both,
and splitting them would force it to correlate a release with its commits over
two round trips for no gain.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.adapters.mock.dataset import DEFAULT_SCENARIO, Scenario
from app.mcp_servers.common import ToolFailure, iso, parse_window, scenario_from_env


class DeploymentOut(BaseModel):
    service: str
    version: str
    environment: str
    deployed_at: str
    commit_sha: str
    deployed_by: str | None = None


class ChangedFileOut(BaseModel):
    path: str
    additions: int
    deletions: int


class CommitOut(BaseModel):
    sha: str
    message: str
    author: str
    committed_at: str
    files: list[ChangedFileOut] = Field(default_factory=list)


class PullRequestOut(BaseModel):
    number: int
    title: str
    author: str
    merged_at: str | None = None
    commits: list[str] = Field(default_factory=list)
    url: str | None = None


def build_server(scenario: Scenario = DEFAULT_SCENARIO) -> MCPServer:
    server = MCPServer(
        name="ops-code",
        version="1.0.0",
        instructions=(
            "Read-only access to deployments, commits and pull requests. "
            "All timestamps are UTC ISO 8601. Nothing here can modify a repository."
        ),
    )

    @server.tool(
        description=(
            "Releases of a service within a window, newest first, each with its "
            "version, deploy time and the commit it was built from."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_recent_deployments(service: str, start: str, end: str) -> list[DeploymentOut]:
        window = parse_window(start, end)
        found = [
            d
            for d in scenario.deployments
            if d.service == service and window[0] <= d.deployed_at <= window[1]
        ]
        return [
            DeploymentOut(
                service=d.service,
                version=d.version,
                environment=d.environment,
                deployed_at=iso(d.deployed_at),
                commit_sha=d.commit_sha,
                deployed_by=d.deployed_by,
            )
            for d in sorted(found, key=lambda d: d.deployed_at, reverse=True)
        ]

    @server.tool(
        description=(
            "Commits in a service repository within a window, newest first, with "
            "author, message and the files each one changed."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_commits(service: str, start: str, end: str) -> list[CommitOut]:
        window = parse_window(start, end)
        del service  # один репозиторий на сценарий в синтетическом мире
        found = [c for c in scenario.commits if window[0] <= c.committed_at <= window[1]]
        return [
            CommitOut(
                sha=c.sha,
                message=c.message,
                author=c.author,
                committed_at=iso(c.committed_at),
                files=[
                    ChangedFileOut(path=f.path, additions=f.additions, deletions=f.deletions)
                    for f in c.files
                ],
            )
            for c in sorted(found, key=lambda c: c.committed_at, reverse=True)
        ]

    @server.tool(
        description=(
            "One pull request by number: title, author, merge time and the commits "
            "it contains. Fails if no such pull request exists."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_pull_request(service: str, number: int) -> PullRequestOut:
        del service
        pr = next((p for p in scenario.pull_requests if p.number == number), None)
        if pr is None:
            raise ToolFailure(f"no pull request #{number}")
        return PullRequestOut(
            number=pr.number,
            title=pr.title,
            author=pr.author,
            merged_at=iso(pr.merged_at) if pr.merged_at else None,
            commits=list(pr.commits),
            url=pr.url,
        )

    @server.resource(
        "code://services",
        name="Deployable services",
        description="Services this code backend knows about, with their latest release.",
        mime_type="application/json",
    )
    def services() -> list[dict[str, str]]:
        latest: dict[str, str] = {}
        for deployment in sorted(scenario.deployments, key=lambda d: d.deployed_at):
            latest[deployment.service] = deployment.version
        return [{"name": name, "latest_version": v} for name, v in sorted(latest.items())]

    return server


def main() -> None:
    build_server(scenario_from_env()).run("stdio")


if __name__ == "__main__":
    main()
