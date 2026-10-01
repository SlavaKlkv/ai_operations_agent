"""Validated settings stored in the durable application database."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AppSetting

MODEL_SETTING_KEY = "model_selection"


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: Literal["light", "standard"]
    label: str
    model_name: str
    download_size_gb: float
    description: str


MODEL_PROFILES = (
    ModelProfile(
        slug="light",
        label="Light",
        model_name="qwen3:4b",
        download_size_gb=2.5,
        description="Для компьютеров с ограниченными ресурсами",
    ),
    ModelProfile(
        slug="standard",
        label="Standard",
        model_name="qwen3:8b",
        download_size_gb=5.2,
        description="Профиль по умолчанию",
    ),
)
PROFILE_BY_SLUG = {profile.slug: profile for profile in MODEL_PROFILES}


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["light", "standard", "custom"]
    model_name: str = Field(min_length=1, max_length=200)
    verified: bool


def default_model_selection(settings: Settings) -> ModelSelection:
    profile = next(
        (item for item in MODEL_PROFILES if item.model_name == settings.llm_model),
        None,
    )
    return ModelSelection(
        profile=profile.slug if profile else "custom",
        model_name=settings.llm_model,
        verified=profile is not None,
    )


async def get_model_selection(session: AsyncSession, settings: Settings) -> ModelSelection:
    row = await session.get(AppSetting, MODEL_SETTING_KEY)
    if row is None:
        return default_model_selection(settings)
    return ModelSelection.model_validate(row.value)


async def save_model_selection(session: AsyncSession, selection: ModelSelection) -> ModelSelection:
    row = await session.get(AppSetting, MODEL_SETTING_KEY)
    payload = selection.model_dump(mode="json")
    if row is None:
        session.add(AppSetting(key=MODEL_SETTING_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()
    return selection
