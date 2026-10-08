"""Эндпоинты подключения GitHub App; токен доступа никогда не раскрывается браузеру."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.security import Principal, current_principal, require_approver
from app.core.config import Settings, get_settings
from app.db.base import get_session
from app.db.models import AppSetting
from app.services.github import GitHubConnectionError, GitHubConnector

router = APIRouter(prefix="/github", tags=["github"])
SELECTED_KEY = "github_selected_repository"
REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepositorySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)


def get_connector(settings: Settings = Depends(get_settings)) -> GitHubConnector:
    # Один коннектор на процесс сохраняет короткоживущий device code между запросами.
    return _connector_for(settings)


_connectors: dict[tuple[str, str, str], GitHubConnector] = {}


def _connector_for(settings: Settings) -> GitHubConnector:
    key = (str(settings.sqlite_path), settings.github_app_client_id, settings.github_app_slug)
    if key not in _connectors:
        _connectors[key] = GitHubConnector(settings)
    return _connectors[key]


async def shutdown_connectors() -> None:
    connectors = list(_connectors.values())
    _connectors.clear()
    for connector in connectors:
        await connector.close()


def _error(exc: GitHubConnectionError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/status")
async def connection_status(
    connector: GitHubConnector = Depends(get_connector),
    session: AsyncSession = Depends(get_session),
    _principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    try:
        credential = await connector.credential()
        installations = await connector.installations() if credential else []
    except GitHubConnectionError as exc:
        return {
            "configured": bool(connector.client_id and connector.app_slug),
            "connected": False,
            "error": str(exc),
        }
    selected = await session.get(AppSetting, SELECTED_KEY)
    selected_value = selected.value if selected else None
    if selected_value and selected_value.get("installation_id") not in {
        item["id"] for item in installations
    }:
        selected_value = None
    return {
        "configured": bool(connector.client_id and connector.app_slug),
        "connected": credential is not None,
        "login": credential.get("login") if credential else None,
        "install_url": (
            f"https://github.com/apps/{connector.app_slug}/installations/new"
            if connector.app_slug
            else None
        ),
        "selected": selected_value,
    }


@router.post("/device/start")
async def start_device_flow(
    connector: GitHubConnector = Depends(get_connector),
    _principal: Principal = Depends(require_approver),
) -> dict[str, Any]:
    try:
        return await connector.start()
    except GitHubConnectionError as exc:
        raise _error(exc) from exc


@router.post("/device/{flow_id}/poll")
async def poll_device_flow(
    flow_id: str,
    connector: GitHubConnector = Depends(get_connector),
    _principal: Principal = Depends(require_approver),
) -> dict[str, Any]:
    try:
        return await connector.poll(flow_id)
    except GitHubConnectionError as exc:
        raise _error(exc) from exc


@router.delete("/device/{flow_id}")
async def cancel_device_flow(
    flow_id: str,
    connector: GitHubConnector = Depends(get_connector),
    _principal: Principal = Depends(require_approver),
) -> dict[str, bool]:
    connector.cancel(flow_id)
    return {"cancelled": True}


@router.get("/installations")
async def list_installations(
    connector: GitHubConnector = Depends(get_connector),
    _principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    try:
        return await connector.installations()
    except GitHubConnectionError as exc:
        raise _error(exc) from exc


@router.get("/installations/{installation_id}/repositories")
async def list_repositories(
    installation_id: int,
    connector: GitHubConnector = Depends(get_connector),
    _principal: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    try:
        return await connector.repositories(installation_id)
    except GitHubConnectionError as exc:
        raise _error(exc) from exc


@router.put("/repository")
async def select_repository(
    selection: RepositorySelection,
    connector: GitHubConnector = Depends(get_connector),
    session: AsyncSession = Depends(get_session),
    _principal: Principal = Depends(require_approver),
) -> dict[str, Any]:
    try:
        repositories = await connector.repositories(selection.installation_id)
    except GitHubConnectionError as exc:
        raise _error(exc) from exc
    repository = next((r for r in repositories if r["id"] == selection.repository_id), None)
    if repository is None:
        raise HTTPException(status_code=404, detail="Репозиторий недоступен для этой установки.")
    value = {"installation_id": selection.installation_id, **repository}
    row = await session.get(AppSetting, SELECTED_KEY)
    if row is None:
        session.add(AppSetting(key=SELECTED_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return value


@router.get("/preview")
async def preview_repository(
    connector: GitHubConnector = Depends(get_connector),
    session: AsyncSession = Depends(get_session),
    _principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    selected = await session.get(AppSetting, SELECTED_KEY)
    if selected is None:
        raise HTTPException(status_code=409, detail="Сначала выберите репозиторий GitHub.")
    repository = selected.value
    name = str(repository.get("full_name", ""))
    installation_id = repository.get("installation_id")
    repository_id = repository.get("id")
    if (
        not REPOSITORY_NAME.fullmatch(name)
        or not isinstance(installation_id, int)
        or not isinstance(repository_id, int)
    ):
        raise HTTPException(status_code=409, detail="Сохранённый репозиторий некорректен.")
    try:
        allowed = await connector.repositories(installation_id)
        if not any(item["id"] == repository_id for item in allowed):
            raise HTTPException(status_code=403, detail="Доступ к репозиторию изменился.")
        commits = await connector.api_request("GET", f"/repos/{name}/commits?per_page=5")
        pulls = await connector.api_request("GET", f"/repos/{name}/pulls?state=all&per_page=5")
        deployments = await connector.api_request("GET", f"/repos/{name}/deployments?per_page=5")
    except GitHubConnectionError as exc:
        raise _error(exc) from exc
    if not all(isinstance(items, list) for items in (commits, pulls, deployments)):
        raise HTTPException(status_code=502, detail="GitHub вернул некорректные данные.")
    return {
        "repository": name,
        "commits": [
            {"sha": item["sha"][:12], "message": item["commit"]["message"].splitlines()[0]}
            for item in commits
        ],
        "pull_requests": [
            {"number": item["number"], "title": item["title"], "state": item["state"]}
            for item in pulls
        ],
        "deployments": [
            {"id": item["id"], "environment": item["environment"], "sha": item["sha"][:12]}
            for item in deployments
        ],
    }


@router.delete("")
async def disconnect(
    connector: GitHubConnector = Depends(get_connector),
    session: AsyncSession = Depends(get_session),
    _principal: Principal = Depends(require_approver),
) -> dict[str, Any]:
    connector.grants.clear()
    connector.store.clear()
    row = await session.get(AppSetting, SELECTED_KEY)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return {"connected": False}
