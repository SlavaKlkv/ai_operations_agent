"""First-run diagnostics and local model selection."""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.github import SELECTED_KEY, get_connector
from app.api.security import Principal, current_principal, require_approver
from app.core.config import Settings, get_settings
from app.db.base import get_session
from app.db.models import AppSetting
from app.mcp.client import MCPToolPool
from app.mcp.runtime import get_pool
from app.mcp_servers.runbooks import load_directory
from app.services.github import GitHubConnectionError, GitHubConnector
from app.services.model_download import ModelDownloadManager
from app.services.ollama import ModelCompatibilityError, OllamaClient, OllamaSnapshot
from app.services.prometheus import PrometheusClient, PrometheusConnectionError
from app.services.settings_store import (
    MODEL_PROFILES,
    PROFILE_BY_SLUG,
    ModelProfile,
    ModelSelection,
    get_model_selection,
    save_model_selection,
)

router = APIRouter(prefix="/setup", tags=["setup"])
download_manager = ModelDownloadManager()


def get_download_manager() -> ModelDownloadManager:
    return download_manager


class CheckView(BaseModel):
    ready: bool
    status: str
    action: str | None = None


class ModelView(BaseModel):
    selection: ModelSelection
    installed: bool
    installed_models: list[str] = Field(default_factory=list)
    profiles: list[ModelProfile]


class SourceView(BaseModel):
    name: str
    ready: bool
    required: bool
    detail: str


class SetupView(BaseModel):
    ready: bool
    ollama: CheckView
    model: ModelView
    storage: CheckView
    github: CheckView
    sources: list[SourceView] = Field(default_factory=list)


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["light", "standard", "custom"]
    model_name: str | None = Field(default=None, min_length=1, max_length=200)


def get_ollama_client(settings: Settings = Depends(get_settings)) -> OllamaClient:
    return OllamaClient(settings.ollama_base_url)


async def _sources(settings: Settings, pool: MCPToolPool) -> list[SourceView]:
    prometheus = await _prometheus_source(settings)
    runbooks = _runbooks_source(settings)
    if not settings.mcp_enabled:
        return [
            prometheus,
            runbooks,
            SourceView(
                name="Реальные операционные источники",
                ready=False,
                required=True,
                detail=(
                    "Сейчас доступен только demo-сценарий. Реальные источники ещё не подключены."
                ),
            ),
        ]
    await pool.connect()
    sources = [
        SourceView(
            name=item.name,
            ready=item.connected,
            required=item.required,
            detail=(
                f"Доступно инструментов: {item.tool_count}"
                if item.connected
                else item.error or "Источник недоступен"
            ),
        )
        for item in pool.status
    ]
    sources.append(prometheus)
    sources.append(runbooks)
    sources.append(
        SourceView(
            name="Реальные операционные источники",
            ready=False,
            required=True,
            detail="Текущие MCP-серверы используют синтетические данные.",
        )
    )
    return sources


def _runbooks_source(settings: Settings) -> SourceView:
    documents = load_directory(settings.runbooks_dir)
    if documents:
        return SourceView(
            name="Локальные runbook",
            ready=True,
            required=False,
            detail=f"Доступно Markdown-runbook: {len(documents)}.",
        )
    return SourceView(
        name="Локальные runbook",
        ready=False,
        required=False,
        detail="Каталог пуст или недоступен; сервер знаний работает в demo-режиме.",
    )


async def _prometheus_source(settings: Settings) -> SourceView:
    if not settings.prometheus_url:
        return SourceView(
            name="Prometheus",
            ready=False,
            required=False,
            detail="Не настроен. Укажите PROMETHEUS_URL для реального мониторинга.",
        )
    try:
        client = PrometheusClient(
            settings.prometheus_url, service_label=settings.prometheus_service_label
        )
        try:
            await client.check()
        finally:
            await client.close()
    except PrometheusConnectionError as exc:
        return SourceView(name="Prometheus", ready=False, required=False, detail=str(exc))
    return SourceView(
        name="Prometheus",
        ready=True,
        required=False,
        detail="Реальный Prometheus доступен для read-only запросов.",
    )


async def _storage_check(session: AsyncSession, settings: Settings) -> CheckView:
    try:
        await session.execute(text("SELECT 1"))
        if settings.storage_backend == "sqlite" and settings.app_env != "test":
            path = settings.sqlite_path
            if not path.is_file() or not os.access(path.parent, os.W_OK):
                raise OSError("database file or writable directory is missing")
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            if not revision:
                raise OSError("database migration version is missing")
        return CheckView(ready=True, status=f"{settings.storage_backend} доступен")
    except Exception:
        return CheckView(
            ready=False,
            status="Хранилище недоступно",
            action="Проверьте постоянный каталог данных, права записи и миграции.",
        )


def _model_view(selection: ModelSelection, snapshot: OllamaSnapshot) -> ModelView:
    return ModelView(
        selection=selection,
        installed=selection.model_name in snapshot.models,
        installed_models=list(snapshot.models),
        profiles=list(MODEL_PROFILES),
    )


@router.get("", response_model=SetupView)
async def setup_status(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    ollama: OllamaClient = Depends(get_ollama_client),
    pool: MCPToolPool = Depends(get_pool),
    github_connector: GitHubConnector = Depends(get_connector),
    _principal: Principal = Depends(current_principal),
) -> SetupView:
    snapshot = await ollama.snapshot()
    selection = await get_model_selection(session, settings)
    model = _model_view(selection, snapshot)

    storage = await _storage_check(session, settings)

    sources = await _sources(settings, pool)
    ollama_check = CheckView(
        ready=snapshot.available,
        status="Ollama доступен" if snapshot.available else "Ollama недоступен",
        action=None if snapshot.available else "Запустите Ollama и проверьте адрес подключения.",
    )
    selected = await session.get(AppSetting, SELECTED_KEY)
    try:
        credential = await github_connector.credential()
        installations = await github_connector.installations() if credential else []
        github_ready = (
            credential is not None
            and selected is not None
            and selected.value.get("installation_id") in {item["id"] for item in installations}
        )
        github_action = None
        if not github_ready:
            github_action = (
                "Общая GitHub App ещё не настроена в этой сборке."
                if not github_connector.client_id or not github_connector.app_slug
                else "Подключите GitHub и выберите репозиторий в этом мастере."
            )
    except GitHubConnectionError as exc:
        github_ready = False
        github_action = str(exc)
    github = CheckView(
        ready=github_ready,
        status="GitHub подключён" if github_ready else "GitHub не подключён",
        action=github_action,
    )
    required_sources_ready = all(item.ready for item in sources if item.required)
    return SetupView(
        ready=(
            ollama_check.ready
            and model.installed
            and storage.ready
            and github.ready
            and required_sources_ready
        ),
        ollama=ollama_check,
        model=model,
        storage=storage,
        github=github,
        sources=sources,
    )


@router.put("/model", response_model=ModelView)
async def select_model(
    update: ModelUpdate,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    ollama: OllamaClient = Depends(get_ollama_client),
    _principal: Principal = Depends(require_approver),
) -> ModelView:
    if update.profile != "custom":
        profile = PROFILE_BY_SLUG[update.profile]
        model_name = profile.model_name
        verified = True
    else:
        model_name = (update.model_name or "").strip()
        if not model_name:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Укажите имя установленной модели Ollama.",
            )
        verified = False

    snapshot = await ollama.snapshot()
    if not snapshot.available:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ollama недоступен. Запустите Ollama и повторите проверку.",
        )
    if model_name not in snapshot.models:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Модель {model_name} не установлена. Сначала загрузите её в Ollama.",
        )
    if update.profile == "custom":
        try:
            await ollama.verify_custom_model(model_name)
        except ModelCompatibilityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"Модель не прошла проверку совместимости: {exc}",
            ) from exc

    selection = await save_model_selection(
        session,
        ModelSelection(
            profile=update.profile,
            model_name=model_name,
            verified=verified,
        ),
    )
    return _model_view(selection, snapshot)


@router.post("/model/download", status_code=status.HTTP_202_ACCEPTED)
async def start_model_download(
    update: ModelUpdate,
    ollama: OllamaClient = Depends(get_ollama_client),
    manager: ModelDownloadManager = Depends(get_download_manager),
    _principal: Principal = Depends(require_approver),
) -> dict[str, str | int | None]:
    if update.profile == "custom":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Через мастер можно загрузить только поддерживаемый профиль.",
        )
    model = PROFILE_BY_SLUG[update.profile].model_name
    try:
        return manager.start(model, ollama).view()
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/model/download")
async def model_download_status(
    manager: ModelDownloadManager = Depends(get_download_manager),
    _principal: Principal = Depends(current_principal),
) -> dict[str, str | int | None]:
    if manager.current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Загрузка не запущена.")
    return manager.current.view()


@router.delete("/model/download")
async def cancel_model_download(
    manager: ModelDownloadManager = Depends(get_download_manager),
    _principal: Principal = Depends(require_approver),
) -> dict[str, str | int | None]:
    if manager.current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Загрузка не запущена.")
    await manager.cancel()
    return manager.current.view()
