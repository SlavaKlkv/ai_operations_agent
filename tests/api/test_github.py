"""GitHub connection API never returns credentials and respects approver scope."""

from __future__ import annotations

from app.api.routes.github import get_connector
from app.core.config import Settings
from app.services.github import GitHubConnector


async def test_connection_selection_and_disconnect(app, client, tmp_path):
    connector = GitHubConnector(
        Settings(
            _env_file=None,
            sqlite_path=tmp_path / "agent.db",
            github_app_client_id="Iv1.test",
            github_app_slug="agent-demo",
        )
    )
    connector.store.write({"access_token": "ghu_sensitive", "expires_at": None, "login": "octocat"})

    async def installations():
        return [{"id": 7, "account": "octocat"}]

    async def repositories(installation_id):
        assert installation_id == 7
        return [{"id": 12, "full_name": "octocat/repo", "private": True}]

    connector.installations = installations
    connector.repositories = repositories

    requested = []

    async def api_request(method, path, *, json_body=None):
        requested.append((method, path))
        if "/commits?" in path:
            return [{"sha": "abcdef1234567890", "commit": {"message": "Fix incident\nDetails"}}]
        if "/pulls?" in path:
            return [{"number": 3, "title": "Fix incident", "state": "closed"}]
        if "/deployments?" in path:
            return [{"id": 4, "environment": "production", "sha": "abcdef1234567890"}]
        raise AssertionError(path)

    connector.api_request = api_request
    app.dependency_overrides[get_connector] = lambda: connector
    try:
        before = await client.get("/github/status")
        assert before.json()["connected"] is True
        assert "ghu_sensitive" not in before.text
        invalid = await client.put(
            "/github/repository", json={"installation_id": 7, "repository_id": 99}
        )
        assert invalid.status_code == 404
        selected = await client.put(
            "/github/repository", json={"installation_id": 7, "repository_id": 12}
        )
        assert selected.status_code == 200
        assert selected.json()["full_name"] == "octocat/repo"
        ready = await client.get("/github/status")
        assert ready.json()["selected"]["id"] == 12
        preview = await client.get("/github/preview")
        assert preview.status_code == 200
        assert preview.json()["repository"] == "octocat/repo"
        assert preview.json()["commits"][0]["message"] == "Fix incident"
        assert all(path.startswith("/repos/octocat/repo/") for _, path in requested)
        setup = await client.get("/setup")
        real_runs = next(
            source
            for source in setup.json()["sources"]
            if source["name"] == "Реальные расследования"
        )
        assert real_runs["ready"] is False
        disconnected = await client.delete("/github")
        assert disconnected.json() == {"connected": False}
        assert connector.store.read() is None
        assert (await client.get("/github/status")).json()["selected"] is None
    finally:
        await connector.close()


async def test_reader_cannot_change_connection(app, reader_client):
    assert (await reader_client.post("/github/device/start")).status_code == 403
    assert (await reader_client.delete("/github")).status_code == 403
    assert (
        await reader_client.put(
            "/github/repository", json={"installation_id": 1, "repository_id": 1}
        )
    ).status_code == 403
