"""GitHub Device Flow, локальное хранение учётных данных и область репозитория."""

from __future__ import annotations

import asyncio
import sys
import time

import httpx
import pytest

from app.core.config import Settings
from app.services.github import CredentialStore, GitHubConnectionError, GitHubConnector


def _connector(tmp_path, handler):
    settings = Settings(
        _env_file=None,
        sqlite_path=tmp_path / "agent.db",
        github_app_client_id="Iv1.example",
        github_app_slug="agent-demo",
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GitHubConnector(settings, client)


async def test_device_flow_obeys_poll_interval_and_never_exposes_token(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "sensitive-device-code",
                    "user_code": "ABCD-EFGH",
                    "interval": 5,
                    "expires_in": 900,
                },
            )
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ghu_sensitive",
                    "refresh_token": "ghr_sensitive",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        raise AssertionError(request.url)

    connector = _connector(tmp_path, handler)
    try:
        grant = await connector.start()
        assert grant["user_code"] == "ABCD-EFGH"
        assert "device_code" not in grant
        assert "sensitive" not in str(grant)
        pending = await connector.poll(grant["flow_id"])
        assert pending["state"] == "pending"
        assert len(calls) == 1
        connector.grants[grant["flow_id"]].next_poll_at = time.monotonic() - 1
        connected = await connector.poll(grant["flow_id"])
        assert connected == {"state": "connected", "login": "octocat"}
        assert "ghu_sensitive" not in connector.store.token_path.read_text(encoding="utf-8")
        assert connector.store.read()["access_token"] == "ghu_sensitive"
        if sys.platform != "win32":
            # На Windows у файлов нет POSIX-бита 0600 — там права задаёт ACL.
            assert connector.store.key_path.stat().st_mode & 0o077 == 0
            assert connector.store.token_path.stat().st_mode & 0o077 == 0
    finally:
        await connector.close()


async def test_refresh_rotates_token_without_client_secret(tmp_path):
    refresh_calls = 0

    def handler(request):
        nonlocal refresh_calls
        refresh_calls += 1
        assert request.url.path == "/login/oauth/access_token"
        assert b"client_secret" not in request.content
        assert b"refresh_token=old-refresh" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    connector = _connector(tmp_path, handler)
    connector.store.write(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": time.time() - 1,
            "refresh_expires_at": time.time() + 100,
            "login": "octocat",
        }
    )
    try:
        first, second = await asyncio.gather(connector.credential(), connector.credential())
        assert first["access_token"] == second["access_token"] == "new-access"
        assert refresh_calls == 1
        assert connector.store.read()["refresh_token"] == "new-refresh"
    finally:
        await connector.close()


async def test_installation_scope_and_readback(tmp_path):
    def handler(request):
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {"id": 7, "app_slug": "agent-demo", "account": {"login": "octocat"}},
                        {"id": 8, "app_slug": "other-app", "account": {"login": "other"}},
                    ]
                },
            )
        if request.url.path == "/user/installations/7/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"id": 12, "full_name": "octocat/repo", "private": True},
                    ]
                },
            )
        raise AssertionError(request.url)

    connector = _connector(tmp_path, handler)
    connector.store.write({"access_token": "ghu_test", "expires_at": None, "login": "octocat"})
    try:
        assert await connector.installations() == [{"id": 7, "account": "octocat"}]
        assert await connector.repositories(7) == [
            {"id": 12, "full_name": "octocat/repo", "private": True}
        ]
        with pytest.raises(GitHubConnectionError):
            await connector.repositories(8)
    finally:
        await connector.close()


async def test_revoked_token_is_reported_without_exposing_it(tmp_path):
    def handler(request):
        assert request.url.path == "/user/installations"
        return httpx.Response(401, json={"message": "Bad credentials"})

    connector = _connector(tmp_path, handler)
    connector.store.write({"access_token": "ghu_secret", "expires_at": None})
    try:
        with pytest.raises(GitHubConnectionError, match="отозван") as raised:
            await connector.installations()
        assert "ghu_secret" not in str(raised.value)
    finally:
        await connector.close()


async def test_permission_failure_is_actionable_and_does_not_expose_token(tmp_path):
    connector = _connector(tmp_path, lambda _request: httpx.Response(403))
    connector.store.write({"access_token": "ghu_private", "expires_at": None})
    try:
        with pytest.raises(GitHubConnectionError, match="разрешения App") as raised:
            await connector.installations()
        assert "ghu_private" not in str(raised.value)
    finally:
        await connector.close()


async def test_slow_down_extends_device_poll_interval(tmp_path):
    def handler(request):
        if request.url.path == "/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device",
                    "user_code": "ABCD-EFGH",
                    "interval": 5,
                    "expires_in": 900,
                },
            )
        return httpx.Response(200, json={"error": "slow_down", "interval": 10})

    connector = _connector(tmp_path, handler)
    try:
        grant = await connector.start()
        connector.grants[grant["flow_id"]].next_poll_at = time.monotonic() - 1
        pending = await connector.poll(grant["flow_id"])
        assert pending == {"state": "pending", "retry_after": 10}
        assert connector.grants[grant["flow_id"]].interval == 10
    finally:
        await connector.close()


async def test_cancelled_device_grant_cannot_save_late_token(tmp_path):
    def handler(request):
        if request.url.path == "/login/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device",
                    "user_code": "ABCD-EFGH",
                    "interval": 5,
                    "expires_in": 900,
                },
            )
        return httpx.Response(200, json={"access_token": "ghu_late"})

    connector = _connector(tmp_path, handler)
    try:
        grant = await connector.start()
        connector.grants[grant["flow_id"]].next_poll_at = time.monotonic() - 1
        connector.cancel(grant["flow_id"])
        with pytest.raises(GitHubConnectionError):
            await connector.poll(grant["flow_id"])
        assert connector.store.read() is None
    finally:
        await connector.close()


def test_disconnect_removes_only_encrypted_credential(tmp_path):
    store = CredentialStore(tmp_path)
    store.write({"access_token": "ghu_test"})
    store.clear()
    assert store.read() is None
    assert store.key_path.exists()
