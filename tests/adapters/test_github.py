"""GitHub providers cannot leave the repository selected by the user."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.github import GitHubCodeProvider, GitHubDataError, GitHubIssueProvider
from app.adapters.mock.providers import DuplicateIssueError
from app.domain.models import IssueDraft


class FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    async def api_request(self, method, path, *, json_body=None):
        self.calls.append((method, path, json_body))
        if path.startswith("/repos/octocat/repo/deployments?"):
            return [
                {
                    "sha": "a" * 40,
                    "ref": "v1.2.3",
                    "environment": "production",
                    "created_at": "2026-10-02T10:00:00Z",
                    "creator": {"login": "octocat"},
                }
            ]
        if path.startswith("/repos/octocat/repo/commits?"):
            return [{"sha": "b" * 40}]
        if path.endswith("/commits/" + "b" * 40):
            return {
                "sha": "b" * 40,
                "commit": {
                    "message": "Fix incident\nMore detail",
                    "author": {"name": "Octo", "date": "2026-10-02T10:01:00Z"},
                },
                "author": {"login": "octocat"},
                "files": [{"filename": "app/main.py", "additions": 2, "deletions": 1}],
            }
        if path.startswith("/repos/octocat/repo/issues?"):
            return [
                {
                    "number": 7,
                    "title": "Existing incident",
                    "body": "Known incident",
                    "state": "open",
                    "created_at": "2026-10-02T10:00:00Z",
                    "user": {"login": "octocat"},
                    "labels": [{"name": "incident"}],
                    "html_url": "https://github.com/octocat/repo/issues/7",
                }
            ]
        if path == "/repos/octocat/repo/issues":
            return {
                "number": 8,
                "title": json_body["title"],
                "body": json_body["body"],
                "state": "open",
                "created_at": "2026-10-02T10:00:00Z",
                "user": {"login": "octocat"},
                "labels": [{"name": name} for name in json_body["labels"]],
            }
        raise AssertionError(path)


async def test_code_provider_reads_only_selected_repository():
    connector = FakeConnector()
    provider = GitHubCodeProvider(connector, "octocat/repo")
    start = datetime(2026, 10, 2, 9, tzinfo=UTC)
    end = start + timedelta(hours=2)

    deployments = await provider.get_recent_deployments("billing-service", start, end)
    commits = await provider.get_commits("billing-service", start, end)

    assert deployments[0].version == "v1.2.3"
    assert commits[0].message == "Fix incident"
    assert commits[0].files[0].path == "app/main.py"
    assert all(path.startswith("/repos/octocat/repo/") for _, path, _ in connector.calls)


async def test_issue_provider_rejects_duplicate_before_write():
    connector = FakeConnector()
    provider = GitHubIssueProvider(connector, "octocat/repo")

    with pytest.raises(DuplicateIssueError):
        await provider.create_issue(
            IssueDraft(title="Existing incident", body="This text is long enough for a draft."),
            author="reviewer",
        )

    assert not any(
        method == "POST" and path.endswith("/issues") for method, path, _ in connector.calls
    )


async def test_issue_provider_creates_only_in_selected_repository():
    connector = FakeConnector()
    provider = GitHubIssueProvider(connector, "octocat/repo")

    issue = await provider.create_issue(
        IssueDraft(
            title="New production incident",
            body="This text is sufficiently long to meet the issue draft contract.",
            labels=["incident"],
        ),
        author="reviewer",
    )

    assert issue.key == "8"
    write = next(call for call in connector.calls if call[0] == "POST")
    assert write[1] == "/repos/octocat/repo/issues"
    assert write[2]["labels"] == ["incident"]


async def test_provider_surfaces_safe_connector_failure():
    class BrokenConnector:
        async def api_request(self, *args, **kwargs):
            from app.services.github import GitHubConnectionError

            raise GitHubConnectionError("GitHub не подключён.")

    provider = GitHubCodeProvider(BrokenConnector(), "octocat/repo")
    now = datetime.now(UTC)
    with pytest.raises(GitHubDataError, match="не подключён"):
        await provider.get_recent_deployments("billing-service", now - timedelta(minutes=1), now)
