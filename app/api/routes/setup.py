"""First-run diagnostics and local model selection."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.security import Principal, current_principal, require_approver
from app.core.config import Settings, get_settings
from app.db.base import get_session
from app.mcp.client import MCPToolPool
from app.mcp.runtime import get_pool
from app.services.ollama import ModelCompatibilityError, OllamaClient, OllamaSnapshot
from app.services.settings_store import (
    MODEL_PROFILES,
    PROFILE_BY_SLUG,
    ModelProfile,
    ModelSelection,
    get_model_selection,
    save_model_selection,
)

router = APIRouter(prefix="/setup", tags=["setup"])


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
    if not settings.mcp_enabled:
        return []
    await pool.connect()
    return [
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
    _principal: Principal = Depends(current_principal),
) -> SetupView:
    snapshot = await ollama.snapshot()
    selection = await get_model_selection(session, settings)
    model = _model_view(selection, snapshot)

    try:
        await session.execute(text("SELECT 1"))
        storage = CheckView(ready=True, status=f"{settings.storage_backend} доступен")
    except Exception:
        storage = CheckView(
            ready=False,
            status="Хранилище недоступно",
            action="Проверьте права на каталог данных и откройте диагностику.",
        )

    sources = await _sources(settings, pool)
    ollama_check = CheckView(
        ready=snapshot.available,
        status="Ollama доступен" if snapshot.available else "Ollama недоступен",
        action=None if snapshot.available else "Запустите Ollama и проверьте адрес подключения.",
    )
    github = CheckView(
        ready=False,
        status="GitHub не подключён",
        action="Подключение через Device Flow будет доступно на следующем этапе.",
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
