"""Typed, repository-scoped providers backed by one GitHub App connection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.adapters.mock.providers import DuplicateIssueError
from app.domain.models import (
    ChangedFile,
    Commit,
    Deployment,
    Issue,
    IssueDraft,
    IssueState,
    PullRequest,
)
from app.services.github import GitHubConnectionError, GitHubConnector


class GitHubDataError(RuntimeError):
    """GitHub responded, but not with data that fits the agent's contract."""


def _at(value: object) -> datetime:
    if not isinstance(value, str):
        raise GitHubDataError("GitHub did not return a timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubDataError("GitHub returned an invalid timestamp.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _login(value: object) -> str:
    return str(value.get("login", "")) if isinstance(value, dict) else ""


class _RepositoryProvider:
    def __init__(self, connector: GitHubConnector, repository: str) -> None:
        self.connector = connector
        self.repository = repository

    async def _request(
        self, method: str, suffix: str, *, json_body: dict[str, Any] | None = None
    ) -> Any:
        try:
            return await self.connector.api_request(
                method, f"/repos/{self.repository}{suffix}", json_body=json_body
            )
        except GitHubConnectionError as exc:
            raise GitHubDataError(str(exc)) from exc


class GitHubCodeProvider(_RepositoryProvider):
    """Read commits, pull requests and deployments from one selected repository."""

    async def get_recent_deployments(
        self, service: str, start: datetime, end: datetime
    ) -> list[Deployment]:
        payload = await self._request("GET", "/deployments?per_page=100")
        if not isinstance(payload, list):
            raise GitHubDataError("GitHub returned an invalid deployments list.")
        found: list[Deployment] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                deployed_at = _at(item.get("created_at") or item.get("updated_at"))
                if not start <= deployed_at <= end:
                    continue
                found.append(
                    Deployment(
                        service=service,
                        version=str(item.get("ref") or item["sha"]),
                        environment=str(item.get("environment") or "production"),
                        deployed_at=deployed_at,
                        commit_sha=str(item["sha"]),
                        deployed_by=_login(item.get("creator")) or None,
                    )
                )
            except (GitHubDataError, KeyError, TypeError):
                continue
        return sorted(found, key=lambda deployment: deployment.deployed_at, reverse=True)

    async def get_commits(self, service: str, since: datetime, until: datetime) -> list[Commit]:
        del service
        query = urlencode(
            {
                "per_page": 30,
                "since": since.astimezone(UTC).isoformat(),
                "until": until.astimezone(UTC).isoformat(),
            }
        )
        payload = await self._request("GET", f"/commits?{query}")
        if not isinstance(payload, list):
            raise GitHubDataError("GitHub returned an invalid commits list.")
        commits: list[Commit] = []
        for summary in payload[:30]:
            if not isinstance(summary, dict) or not isinstance(summary.get("sha"), str):
                continue
            detail = await self._request("GET", f"/commits/{summary['sha']}")
            if not isinstance(detail, dict):
                continue
            try:
                commit_data = detail["commit"]
                author_data = commit_data.get("author", {})
                files = detail.get("files", [])
                commits.append(
                    Commit(
                        sha=str(detail["sha"]),
                        message=str(commit_data["message"]).splitlines()[0],
                        author=_login(detail.get("author"))
                        or str(author_data.get("name") or "unknown"),
                        committed_at=_at(author_data.get("date")),
                        files=tuple(
                            ChangedFile(
                                path=str(file["filename"]),
                                additions=int(file.get("additions", 0)),
                                deletions=int(file.get("deletions", 0)),
                            )
                            for file in files[:100]
                            if isinstance(file, dict) and isinstance(file.get("filename"), str)
                        ),
                    )
                )
            except (GitHubDataError, KeyError, TypeError, ValueError):
                continue
        return sorted(commits, key=lambda commit: commit.committed_at, reverse=True)

    async def get_pull_request(self, service: str, number: int) -> PullRequest | None:
        del service
        try:
            payload = await self._request("GET", f"/pulls/{number}")
        except GitHubDataError as exc:
            if "не найден" in str(exc):
                return None
            raise
        if not isinstance(payload, dict):
            raise GitHubDataError("GitHub returned an invalid pull request.")
        commits = await self._request("GET", f"/pulls/{number}/commits?per_page=100")
        try:
            return PullRequest(
                number=int(payload["number"]),
                title=str(payload["title"]),
                author=_login(payload.get("user")) or "unknown",
                merged_at=_at(payload["merged_at"]) if payload.get("merged_at") else None,
                commits=tuple(
                    str(item["sha"])
                    for item in commits
                    if isinstance(item, dict) and isinstance(item.get("sha"), str)
                )
                if isinstance(commits, list)
                else (),
                url=str(payload.get("html_url")) if payload.get("html_url") else None,
            )
        except (GitHubDataError, KeyError, TypeError, ValueError) as exc:
            raise GitHubDataError("GitHub returned an invalid pull request.") from exc


class GitHubIssueProvider(_RepositoryProvider):
    """Read and, only after graph approval, write GitHub issues."""

    async def search_issues(
        self, query: str, service: str | None = None, state: str | None = None
    ) -> list[Issue]:
        del service
        requested_state = state if state in {"open", "closed"} else "open"
        payload = await self._request(
            "GET", f"/issues?{urlencode({'state': requested_state, 'per_page': 100})}"
        )
        if not isinstance(payload, list):
            raise GitHubDataError("GitHub returned an invalid issues list.")
        needle = query.casefold()
        return [
            issue
            for item in payload
            if isinstance(item, dict)
            and "pull_request" not in item
            and (issue := _issue(item, service=None)) is not None
            and (not needle or needle in issue.title.casefold() or needle in issue.body.casefold())
        ]

    async def get_issue(self, key: str) -> Issue | None:
        try:
            number = int(key.lstrip("#"))
        except ValueError:
            return None
        try:
            payload = await self._request("GET", f"/issues/{number}")
        except GitHubDataError as exc:
            if "не найден" in str(exc):
                return None
            raise
        return _issue(payload, service=None) if isinstance(payload, dict) else None

    async def create_issue(self, draft: IssueDraft, *, author: str) -> Issue:
        del author
        existing = await self.search_issues(draft.title, service=draft.service, state="open")
        if any(issue.title.casefold() == draft.title.casefold() for issue in existing):
            raise DuplicateIssueError(f"an open issue titled {draft.title!r} already exists")
        payload = await self._request(
            "POST",
            "/issues",
            json_body={"title": draft.title, "body": draft.body, "labels": draft.labels},
        )
        if not isinstance(payload, dict):
            raise GitHubDataError("GitHub returned an invalid created issue.")
        issue = _issue(payload, service=draft.service)
        if issue is None:
            raise GitHubDataError("GitHub returned an invalid created issue.")
        return issue

    async def add_issue_comment(self, key: str, text: str, *, author: str) -> Issue:
        del author
        try:
            number = int(key.lstrip("#"))
        except ValueError as exc:
            raise GitHubDataError("Некорректный номер GitHub issue.") from exc
        await self._request("POST", f"/issues/{number}/comments", json_body={"body": text})
        issue = await self.get_issue(str(number))
        if issue is None:
            raise GitHubDataError("GitHub не вернул issue после добавления комментария.")
        return issue


def _issue(payload: dict[str, Any], service: str | None) -> Issue | None:
    try:
        labels = payload.get("labels", [])
        return Issue(
            key=str(payload["number"]),
            title=str(payload["title"]),
            body=str(payload.get("body") or ""),
            service=service,
            labels=tuple(
                str(label["name"])
                for label in labels
                if isinstance(label, dict) and isinstance(label.get("name"), str)
            ),
            state=IssueState(str(payload.get("state", "open"))),
            created_at=_at(payload["created_at"]) if payload.get("created_at") else None,
            created_by=_login(payload.get("user")),
            url=str(payload.get("html_url")) if payload.get("html_url") else None,
        )
    except (GitHubDataError, KeyError, TypeError, ValueError):
        return None
