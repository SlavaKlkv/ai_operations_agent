"""Хранение запусков агента: превращение терминального состояния графа в строки."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.state import AgentState, ApprovalState, RunStatus
from app.db.base import utcnow
from app.db.models import (
    AgentRun,
    Approval,
    AuditEvent,
    IncidentAnalysisRecord,
    ToolCall,
)


class NoPendingApproval(LookupError):
    """Решение пришло для запуска, который его не ждёт."""


async def create_run(
    session: AsyncSession,
    *,
    task: str,
    target_service: str | None = None,
    model_name: str | None = None,
    actor: str = "system",
) -> AgentRun:
    run = AgentRun(
        task=task,
        target_service=target_service,
        model_name=model_name,
        status=RunStatus.RUNNING,
        started_at=utcnow(),
    )
    session.add(run)
    await session.flush()
    session.add(
        AuditEvent(
            run_id=run.id,
            at=utcnow(),
            actor=actor,
            action="run.created",
            detail={"task": task},
        )
    )
    await session.commit()
    return run


async def persist_progress(
    session: AsyncSession,
    run: AgentRun,
    state: AgentState,
    *,
    pending: dict[str, Any] | None = None,
) -> AgentRun:
    """Записать то, что запуск произвёл на данный момент, в одной транзакции.

    Вызывается после каждого вызова графа, а не только после последнего — запуск,
    который приостанавливается для подтверждения, уже произвёл полный анализ и
    предложение, и потерять их из-за незавершённости рабочего процесса означало бы
    свести на нет смысл долговечной паузы.

    pending — это полезная нагрузка прерывания, когда граф остановился на шлюзе
    подтверждения. Она создаёт строку подтверждения: строка существует до
    выполнения действия и является единственным, что его санкционирует.
    """
    if pending is not None:
        session.add(
            Approval(
                run_id=run.id,
                tool=str(pending.get("tool", "")),
                arguments=dict(pending.get("arguments") or {}),
                rationale=str(pending.get("rationale", "")),
                state=ApprovalState.PENDING,
            )
        )
        state = {**state, "status": RunStatus.AWAITING_APPROVAL}
    return await _persist(session, run, state)


async def persist_final_state(session: AsyncSession, run: AgentRun, state: AgentState) -> AgentRun:
    """Записать всё, что произвёл запуск, в одной транзакции."""
    return await _persist(session, run, state)


async def record_decision(
    session: AsyncSession,
    run: AgentRun,
    *,
    approved: bool,
    decided_by: str,
    note: str = "",
) -> Approval:
    """Зафиксировать решение человека до того, как действие будет предпринято.

    Записывается первым и коммитится, чтобы журнал аудита показывал решение даже
    если выполнение действия затем провалится — на вопрос «кто это одобрил»
    нужно отвечать независимо от того, сработало ли оно.
    """
    approval = await _approval_in(session, run.id, ApprovalState.PENDING)
    if approval is None:
        raise NoPendingApproval(f"run {run.id} has no approval awaiting a decision")

    approval.state = ApprovalState.APPROVED if approved else ApprovalState.REJECTED
    approval.decided_by = decided_by
    approval.decided_at = utcnow()
    approval.decision_note = note
    session.add(
        AuditEvent(
            run_id=run.id,
            at=utcnow(),
            actor=decided_by,
            action="approval.approved" if approved else "approval.rejected",
            detail={"tool": approval.tool, "note": note, "approval_id": str(approval.id)},
        )
    )
    await session.commit()
    return approval


async def _persist(session: AsyncSession, run: AgentRun, state: AgentState) -> AgentRun:
    run.status = state.get("status", RunStatus.COMPLETED)
    run.approval_state = state.get("approval_state", run.approval_state)
    run.target_service = state.get("target_service") or run.target_service
    run.step_count = state.get("step_count", 0)
    run.tool_call_count = state.get("tool_call_count", 0)
    run.total_tokens = state.get("input_tokens", 0) + state.get("output_tokens", 0)
    run.final_result = state.get("final_result")
    run.finished_at = None if run.status is RunStatus.AWAITING_APPROVAL else utcnow()
    run.state_snapshot = serialise_state(state)

    # Сохраняем только новые для этого вызова обращения к инструментам. После
    # подтверждения состояние воспроизводится целиком, поэтому без проверки
    # задвоились бы все строки, созданные до паузы расследования.
    already = (
        await session.scalar(
            select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id)
        )
        or 0
    )
    for record in state.get("tool_calls", [])[already:]:
        session.add(
            ToolCall(
                run_id=run.id,
                tool=record.tool,
                arguments=record.arguments,
                started_at=record.started_at,
                duration_ms=record.duration_ms,
                attempt=record.attempt,
                ok=record.ok,
                error=record.error,
                result_summary=record.result_summary,
                cached=record.cached,
            )
        )

    stored_analyses = await session.scalar(
        select(func.count())
        .select_from(IncidentAnalysisRecord)
        .where(IncidentAnalysisRecord.run_id == run.id)
    )
    analysis = state.get("analysis")
    if analysis is not None and not stored_analyses:
        session.add(
            IncidentAnalysisRecord(
                run_id=run.id,
                service=analysis.service,
                incident_start=analysis.incident_start,
                confidence=analysis.confidence,
                summary=analysis.summary,
                payload=analysis.model_dump(mode="json"),
            )
        )

    approved = await _approval_in(session, run.id, ApprovalState.APPROVED)
    if approved is not None and approved.execution_result is None:
        approved.execution_result = state.get("action_result")

    session.add(
        AuditEvent(
            run_id=run.id,
            at=utcnow(),
            actor="agent",
            action=(
                "run.awaiting_approval"
                if run.status is RunStatus.AWAITING_APPROVAL
                else "run.finished"
            ),
            detail={
                "status": str(run.status),
                "tool_calls": run.tool_call_count,
                "llm_calls": state.get("llm_calls", 0),
                "total_tokens": run.total_tokens,
            },
        )
    )
    await session.commit()
    return run


async def _approval_in(
    session: AsyncSession, run_id: uuid.UUID, state: ApprovalState
) -> Approval | None:
    """Загрузить подтверждение по состоянию, не трогая ленивую связь.

    run.approvals вызвал бы синхронную ленивую загрузку внутри асинхронного
    кода, от которой asyncpg отказывается. Явный запрос также делает очевидным,
    что «ожидающее подтверждение» — это факт базы данных, а не что-то закэшированное
    на объекте, который может устареть.
    """
    stmt = select(Approval).where(Approval.run_id == run_id, Approval.state == state)
    return (await session.execute(stmt)).scalars().first()


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> AgentRun | None:
    stmt = (
        select(AgentRun)
        .where(AgentRun.id == run_id)
        .options(
            selectinload(AgentRun.tool_calls),
            selectinload(AgentRun.analyses),
            selectinload(AgentRun.approvals),
        )
        # Иначе запуск из identity map сессии вернётся с коллекциями на момент
        # первой загрузки. Сразу после возобновления будет показано состояние
        # до подтверждённого действия — устаревшее именно в критический момент.
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_runs(session: AsyncSession, *, limit: int = 50) -> list[AgentRun]:
    stmt = select(AgentRun).order_by(AgentRun.created_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars())


async def delete_run(session: AsyncSession, run: AgentRun) -> None:
    """Удалить расследование из истории.

    Дочерние строки — вызовы инструментов, анализы и подтверждения — удаляются
    вместе с запуском по каскаду связи. Журнал аудита удаление переживает: запись
    о том, кто и что решил, должна оставаться доступной и после того, как само
    расследование убрано, поэтому ссылку на запуск обнуляем явным запросом, а не
    полагаемся на ondelete=SET NULL: не всякая локальная база выполняется с
    включённой проверкой внешних ключей.

    Запуск передаётся уже загруженным с коллекциями (см. get_run): каскад по
    связи требует, чтобы дочерние строки были известны сессии.
    """
    await session.execute(update(AuditEvent).where(AuditEvent.run_id == run.id).values(run_id=None))
    await session.delete(run)
    await session.commit()


def serialise_state(state: AgentState) -> dict[str, Any]:
    """Безопасный для JSON снимок состояния, используемый для воспроизведения и отладки.

    Две вещи, которые наивная версия делала неверно. LangGraph кладёт собственную
    служебную информацию в возвращённое состояние под dunder-ключами —
    __interrupt__ несёт объекты, которые не сериализуются — а это внутренности
    фреймворка, не часть истории запуска. И нераспознанный объект раньше проходил
    насквозь нетронутым, поэтому новое поле состояния могло превратить успешный
    запуск в неудачную запись в базу; запасной вариант через repr вместо этого
    сохраняет честность снимка о том, что он не смог представить.
    """

    def encode(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, list | tuple):
            return [encode(v) for v in value]
        if isinstance(value, dict):
            return {k: encode(v) for k, v in value.items()}
        if isinstance(value, uuid.UUID):
            return str(value)
        if isinstance(value, str | int | float | bool) or value is None:
            return value
        return repr(value)

    return {key: encode(value) for key, value in state.items() if not key.startswith("__")}
