"""Явное состояние рабочего процесса.

Состояние — единственный источник правды для запуска. Ничто важное не живёт
только внутри промпта: каждое наблюдение, вызов инструмента и решение графа
записываются здесь, и именно это делает запуск воспроизводимым и проверяемым.
"""

from __future__ import annotations

import operator
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.base import ToolRequest
from app.domain.models import (
    Alert,
    Commit,
    Deployment,
    ErrorGroup,
    Evidence,
    Hypothesis,
    IncidentAnalysis,
    MetricSeries,
)


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class ApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ToolCallRecord(BaseModel):
    """Один выполненный вызов инструмента, сохранённый для аудита и оценки."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    duration_ms: float
    ok: bool
    error: str | None = None
    result_summary: str = ""
    attempt: int = 1
    #: True, если результат получен из кэша, а не от провайдера.
    #: Сохраняется, чтобы оценка отличала дешёвый запуск от просто быстрого.
    cached: bool = False


class ProposedAction(BaseModel):
    """Операция записи, которую агент хочет выполнить, в ожидании подтверждения."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    requires_approval: bool = True


class CollectedContext(BaseModel):
    """Сырые, но типизированные наблюдения для повторного анализа в поздних узлах.

    Доказательства — это читаемая человеком трасса найденного; это её
    машиночитаемый аналог, по которому фактически считают корреляция и анализ.
    """

    model_config = ConfigDict(extra="forbid")

    metrics: dict[str, MetricSeries] = Field(default_factory=dict)
    deployments: list[Deployment] = Field(default_factory=list)
    commits: list[Commit] = Field(default_factory=list)
    error_groups: list[ErrorGroup] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)


class RunError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: str
    kind: str
    message: str
    recoverable: bool = True


class AgentState(TypedDict, total=False):
    """Состояние LangGraph. Редьюсеры делают конкурентные/циклические записи аддитивными."""

    # Входные данные
    run_id: str
    task: str
    target_service: str | None
    window_start: datetime | None
    window_end: datetime | None

    # Накопленные наблюдения
    observations: Annotated[list[dict[str, Any]], operator.add]
    tool_calls: Annotated[list[ToolCallRecord], operator.add]
    evidence: Annotated[list[Evidence], operator.add]
    hypotheses: list[Hypothesis]
    context: CollectedContext
    errors: Annotated[list[RunError], operator.add]

    # Управление
    current_step: str
    step_count: int
    tool_call_count: int
    status: RunStatus
    #: Сколько вызовов ещё разрешают ограничения. Планировщик видит значение,
    #: чтобы тратить ограниченный бюджет на наиболее важные доказательства.
    tool_budget_remaining: int
    #: Итерации цикла «выбор → выполнение → оценка».
    loop_iterations: int
    #: Запрос планировщика между принятием решения и его выполнением.
    pending_requests: list[ToolRequest]
    #: Объяснение остановки словами планировщика. Часть ответа аудита на вопрос,
    #: почему агент завершил работу именно здесь.
    planner_rationale: str

    # Учёт использования модели
    llm_calls: Annotated[int, operator.add]
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]

    # Выходные данные
    analysis: IncidentAnalysis | None
    proposed_actions: list[ProposedAction]
    approval_state: ApprovalState
    #: Кто принял решение и что ответил. Сохраняется в запуске, потому что фраза
    #: «агент создал задачу» не отвечает полностью на вопрос «кто это сделал».
    approved_by: str | None
    approval_note: str
    #: Результат выполненной записи связывает эффект с подтверждением,
    #: а не только со строкой журнала.
    action_result: dict[str, Any] | None
    final_result: str | None


def initial_state(run_id: str, task: str, target_service: str | None = None) -> AgentState:
    return AgentState(
        run_id=run_id,
        task=task,
        target_service=target_service,
        observations=[],
        tool_calls=[],
        evidence=[],
        hypotheses=[],
        context=CollectedContext(),
        errors=[],
        current_step="start",
        step_count=0,
        tool_call_count=0,
        status=RunStatus.RUNNING,
        tool_budget_remaining=0,
        loop_iterations=0,
        pending_requests=[],
        planner_rationale="",
        llm_calls=0,
        input_tokens=0,
        output_tokens=0,
        analysis=None,
        proposed_actions=[],
        approval_state=ApprovalState.NOT_REQUIRED,
        approved_by=None,
        approval_note="",
        action_result=None,
        final_result=None,
    )
