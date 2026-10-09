"""Модели HTTP-запросов и ответов. Намеренно отделены от доменных моделей: формат
обмена может развиваться, не увлекая за собой типы агента."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agent.state import ApprovalState, RunStatus
from app.domain.models import IncidentAnalysis


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=8, max_length=2000, description="What to investigate, in prose.")
    target_service: str | None = Field(
        default=None,
        max_length=200,
        description="Optional override; otherwise inferred from the task.",
    )


class ToolCallView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool: str
    arguments: dict
    started_at: datetime
    duration_ms: float
    attempt: int
    ok: bool
    cached: bool = False
    error: str | None = None
    result_summary: str = ""


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task: str
    target_service: str | None
    model_name: str | None = None
    status: RunStatus
    approval_state: ApprovalState
    step_count: int
    tool_call_count: int
    total_tokens: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    final_result: str | None = None


class PendingApproval(BaseModel):
    """Запись, которой ждёт запуск. Показывается полностью: проверяющий одобряет
    содержимое, а не описание содержимого."""

    model_config = ConfigDict(from_attributes=True)

    approval_id: uuid.UUID
    tool: str
    arguments: dict
    rationale: str = ""


class ApprovalDecision(BaseModel):
    """Решение человека: «да» или «нет», и опционально причина.

    Намеренно не способно выразить что делать — действием является то, что граф
    сохранил в чекпоинт, поэтому подтверждение нельзя перенаправить на содержимое,
    которого проверяющий не видел. И намеренно не способно указать кто — личность
    берётся из учётных данных, потому что имя в теле запроса это метка, а не
    личность.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    note: str = Field(default="", max_length=2000, description="Why, for the audit trail.")


class RunDetail(RunSummary):
    analysis: IncidentAnalysis | None = None
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    pending_approval: PendingApproval | None = None
    approved_by: str | None = None
    action_result: dict | None = None


class TraceStep(BaseModel):
    """Одно наблюдаемое действие рабочего процесса."""

    step: int
    node: str
    detail: dict = Field(default_factory=dict)


class RunTrace(BaseModel):
    """Воспроизводимая история запуска: узлы, инструменты, ветви и сбои."""

    run_id: uuid.UUID
    status: RunStatus
    steps: list[TraceStep] = Field(default_factory=list)
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    durable_approvals: bool = False
    authentication: bool = True
    storage_backend: str
    cache_backend: str
    checkpointer: str
