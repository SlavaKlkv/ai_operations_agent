"""Persistent schema.

What is stored is deliberately narrow: the decisions a run made, the tool calls
it issued, the evidence it relied on, and every approval. Hidden model
reasoning is not stored — the audit question this schema answers is "what did
the agent do and on what basis", not "what did it think".
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.agent.state import ApprovalState, RunStatus
from app.db.base import Base, TimestampMixin, new_uuid

#: JSONB в PostgreSQL и обычный JSON в SQLite, чтобы тесты работали без сервера.
JSONType = JSON().with_variant(JSONB(), "postgresql")
UUIDType = UUID(as_uuid=True)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Кто может подтверждать запись. Пользователи только для чтения могут запускать задачи.
    can_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: SHA-256 API-токена. Сам токен показывается один раз при выпуске и не хранится,
    #: чтобы утечка базы не раскрыла действующие учётные данные.
    api_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    runs: Mapped[list[AgentRun]] = relationship(back_populates="user")


class AppSetting(Base, TimestampMixin):
    """One validated local setting persisted independently from the container."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, object]] = mapped_column(JSONType, nullable=False, default=dict)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_runs_service_created", "target_service", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    task: Mapped[str] = mapped_column(Text, nullable=False)
    target_service: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: Зафиксировано при старте: смена профиля не меняет уже начатое расследование.
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, native_enum=False, length=32), nullable=False, default=RunStatus.RUNNING
    )
    approval_state: Mapped[ApprovalState] = mapped_column(
        SAEnum(ApprovalState, native_enum=False, length=32),
        nullable=False,
        default=ApprovalState.NOT_REQUIRED,
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    final_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Полное конечное состояние для проверки запуска без повторного выполнения.
    state_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    user: Mapped[User | None] = relationship(back_populates="runs")
    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    analyses: Mapped[list[IncidentAnalysisRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ToolCall(Base):
    """One tool invocation. Arguments are stored; results only as a summary,
    because raw tool output can be large and is reproducible from the source."""

    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_run_started", "run_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )

    tool: Mapped[str] = mapped_column(String(200), nullable=False)
    node: Mapped[str | None] = mapped_column(String(200), nullable=True)
    arguments: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Получено из кэша, а не от провайдера. Позволяет аудиту различить реальные
    #: обращения агента к внешней системе и повторно использованные данные.
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class IncidentAnalysisRecord(Base, TimestampMixin):
    __tablename__ = "incident_analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    service: Mapped[str] = mapped_column(String(200), nullable=False)
    incident_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Проверенный IncidentAnalysis, сохранённый без изменений.
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    run: Mapped[AgentRun] = relationship(back_populates="analyses")


class Approval(Base, TimestampMixin):
    """A human decision on a proposed write action.

    The row is created *before* the action runs and is the only thing that
    authorises it; the executor refuses to act without an approved row.
    """

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[ApprovalState] = mapped_column(
        SAEnum(ApprovalState, native_enum=False, length=32),
        nullable=False,
        default=ApprovalState.PENDING,
    )
    decided_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Результат выполненного действия, связывающий подтверждение с эффектом.
    execution_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="approvals")


class AuditEvent(Base):
    """Append-only trail of everything security-relevant that happened."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_run_at", "run_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=new_uuid)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
