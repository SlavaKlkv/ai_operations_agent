"""Local GitHub App Device Flow and encrypted user-token storage."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubConnectionError(Exception):
    """A safe-to-display GitHub connection failure without secret payloads."""


@dataclass
class DeviceGrant:
    device_code: str
    user_code: str
    expires_at: float
    interval: int
    next_poll_at: float


class CredentialStore:
    """Keep ciphertext and its 0600 key in separate files beside the SQLite volume."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.key_path = directory / "github-token.key"
        self.token_path = directory / "github-token.enc"

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            if os.name == "posix":
                os.chmod(path, 0o600)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _cipher(self, *, create: bool) -> Fernet | None:
        if not self.key_path.exists():
            if not create:
                return None
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
        try:
            return Fernet(self.key_path.read_bytes())
        except ValueError as exc:
            raise GitHubConnectionError("Ключ шифрования GitHub повреждён.") from exc

    def read(self) -> dict[str, Any] | None:
        if not self.token_path.exists():
            return None
        cipher = self._cipher(create=False)
        if cipher is None:
            raise GitHubConnectionError(
                "Ключ шифрования GitHub не найден. Подключите GitHub заново."
            )
        try:
            decoded = json.loads(cipher.decrypt(self.token_path.read_bytes()))
            if not isinstance(decoded, dict):
                raise ValueError("credential must be an object")
            return decoded
        except (InvalidToken, ValueError) as exc:
            raise GitHubConnectionError("Не удалось прочитать подключение GitHub.") from exc

    def write(self, payload: dict[str, Any]) -> None:
        cipher = self._cipher(create=True)
        assert cipher is not None
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".github-token-{secrets.token_hex(8)}"
        try:
            self._write_private(temporary, cipher.encrypt(json.dumps(payload).encode()))
            os.replace(temporary, self.token_path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        self.token_path.unlink(missing_ok=True)


class GitHubConnector:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.client_id = settings.github_app_client_id
        self.app_slug = settings.github_app_slug
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.store = CredentialStore(settings.sqlite_path.parent)
        self.grants: dict[str, DeviceGrant] = {}
        self._refresh_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self.client.post(
                f"{GITHUB_WEB}{path}",
                data=data,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("unexpected response")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubConnectionError("GitHub недоступен. Повторите попытку позже.") from exc

    async def start(self) -> dict[str, Any]:
        if not self.client_id or not self.app_slug:
            raise GitHubConnectionError("Общая GitHub App ещё не настроена.")
        payload = await self._post("/login/device/code", {"client_id": self.client_id})
        if "device_code" not in payload or "user_code" not in payload:
            raise GitHubConnectionError("GitHub не выдал код подключения.")
        flow_id = secrets.token_urlsafe(24)
        now = time.monotonic()
        interval = max(5, int(payload.get("interval", 5)))
        self.grants.clear()
        self.grants[flow_id] = DeviceGrant(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            expires_at=now + min(int(payload.get("expires_in", 900)), 900),
            interval=interval,
            next_poll_at=now + interval,
        )
        return {
            "flow_id": flow_id,
            "user_code": str(payload["user_code"]),
            "verification_uri": "https://github.com/login/device",
            "expires_in": min(int(payload.get("expires_in", 900)), 900),
            "interval": interval,
        }

    async def poll(self, flow_id: str) -> dict[str, Any]:
        grant = self.grants.get(flow_id)
        if grant is None:
            raise GitHubConnectionError("Подключение не найдено. Начните заново.")
        now = time.monotonic()
        if now >= grant.expires_at:
            self.grants.pop(flow_id, None)
            return {"state": "expired"}
        if now < grant.next_poll_at:
            return {"state": "pending", "retry_after": max(1, int(grant.next_poll_at - now) + 1)}
        grant.next_poll_at = now + grant.interval
        payload = await self._post(
            "/login/oauth/access_token",
            {
                "client_id": self.client_id,
                "device_code": grant.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if self.grants.get(flow_id) is not grant:
            return {"state": "cancelled"}
        error = payload.get("error")
        if error == "authorization_pending":
            return {"state": "pending", "retry_after": grant.interval}
        if error == "slow_down":
            grant.interval = max(grant.interval + 5, int(payload.get("interval", 0)))
            grant.next_poll_at = time.monotonic() + grant.interval
            return {"state": "pending", "retry_after": grant.interval}
        if error:
            self.grants.pop(flow_id, None)
            return {"state": "denied" if error == "access_denied" else "expired"}
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GitHubConnectionError("GitHub не выдал токен доступа.")
        credential = self._credential(payload)
        try:
            identity = await self._get("/user", token)
        except GitHubConnectionError:
            self.grants.pop(flow_id, None)
            raise
        login = identity.get("login")
        if not isinstance(login, str) or not login:
            self.grants.pop(flow_id, None)
            raise GitHubConnectionError("GitHub не подтвердил аккаунт.")
        credential["login"] = login
        self.store.write(credential)
        self.grants.pop(flow_id, None)
        return {"state": "connected", "login": credential["login"]}

    def cancel(self, flow_id: str) -> None:
        self.grants.pop(flow_id, None)

    @staticmethod
    def _credential(payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        return {
            "access_token": payload["access_token"],
            "expires_at": now + int(payload["expires_in"]) if "expires_in" in payload else None,
            "refresh_token": payload.get("refresh_token"),
            "refresh_expires_at": (
                now + int(payload["refresh_token_expires_in"])
                if "refresh_token_expires_in" in payload
                else None
            ),
        }

    async def _get(self, path: str, token: str) -> dict[str, Any]:
        payload = await self._api_request("GET", path, token=token)
        if not isinstance(payload, dict):
            raise GitHubConnectionError("GitHub вернул некорректные данные.")
        return payload

    async def _api_request(
        self, method: str, path: str, *, token: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubConnectionError("Недопустимый путь GitHub API.")
        try:
            response = await self.client.request(
                method,
                f"{GITHUB_API}{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                json=json_body,
            )
            if response.status_code == 401:
                raise GitHubConnectionError("Доступ GitHub отозван. Подключите аккаунт заново.")
            if response.status_code == 403:
                raise GitHubConnectionError(
                    "GitHub отказал в доступе. Проверьте разрешения App и лимит API."
                )
            if response.status_code == 404:
                raise GitHubConnectionError(
                    "Ресурс GitHub не найден. Проверьте установку и выбранный репозиторий."
                )
            if response.status_code == 429:
                raise GitHubConnectionError("Лимит GitHub API достигнут. Повторите позже.")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubConnectionError("Не удалось проверить доступ к GitHub.") from exc

    async def api_request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> Any:
        stored = await self.credential()
        if stored is None:
            raise GitHubConnectionError("GitHub не подключён.")
        return await self._api_request(
            method, path, token=stored["access_token"], json_body=json_body
        )

    async def credential(self) -> dict[str, Any] | None:
        async with self._refresh_lock:
            return await self._credential_locked()

    async def _credential_locked(self) -> dict[str, Any] | None:
        stored = self.store.read()
        if stored is None:
            return None
        expires_at = stored.get("expires_at")
        if expires_at is not None and time.time() >= float(expires_at) - 60:
            if not stored.get("refresh_token") or (
                stored.get("refresh_expires_at") is not None
                and time.time() >= float(stored["refresh_expires_at"])
            ):
                raise GitHubConnectionError("Срок доступа GitHub истёк. Подключите аккаунт заново.")
            payload = await self._post(
                "/login/oauth/access_token",
                {
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": stored["refresh_token"],
                },
            )
            if "access_token" not in payload:
                raise GitHubConnectionError(
                    "Не удалось обновить доступ GitHub. Подключите аккаунт заново."
                )
            updated = self._credential(payload)
            updated["login"] = stored.get("login", "")
            self.store.write(updated)
            return updated
        return stored

    async def installations(self) -> list[dict[str, Any]]:
        stored = await self.credential()
        if stored is None:
            return []
        found = await self._pages("/user/installations", "installations", stored["access_token"])
        return [
            {"id": item["id"], "account": item["account"]["login"]}
            for item in found
            if item.get("app_slug") == self.app_slug
        ]

    async def repositories(self, installation_id: int) -> list[dict[str, Any]]:
        if installation_id not in {item["id"] for item in await self.installations()}:
            raise GitHubConnectionError("Эта установка GitHub App недоступна.")
        stored = await self.credential()
        assert stored is not None
        found = await self._pages(
            f"/user/installations/{installation_id}/repositories",
            "repositories",
            stored["access_token"],
        )
        return [
            {"id": item["id"], "full_name": item["full_name"], "private": item["private"]}
            for item in found
        ]

    async def _pages(self, path: str, key: str, token: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, 11):
            data = await self._get(f"{path}?per_page=100&page={page}", token)
            items = data.get(key, [])
            if not isinstance(items, list):
                raise GitHubConnectionError("GitHub вернул некорректный список ресурсов.")
            found.extend(items)
            if len(items) < 100 or (
                data.get("total_count") is not None and len(found) >= int(data["total_count"])
            ):
                return found
        raise GitHubConnectionError("Слишком много установок или репозиториев GitHub.")
