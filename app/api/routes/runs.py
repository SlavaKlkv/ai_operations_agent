"""Эндпоинты запусков агента."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from functools import lru_cache
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.empty import NoLogProvider
from app.adapters.github import GitHubCodeProvider, GitHubIssueProvider
from app.adapters.runbooks import LocalRunbookProvider
from app.agent.checkpointing import get_saver
from app.agent.graph import build_graph, run_config
from app.agent.llm import build_chat_model
from app.agent.state import AgentState, ApprovalState, RunStatus, initial_state
from app.api.routes.github import SELECTED_KEY, _connector_for
from app.api.schemas import (
    ApprovalDecision,
    PendingApproval,
    RunDetail,
    RunRequest,
    RunSummary,
    RunTrace,
    ToolCallView,
    TraceStep,
)
from app.api.security import Principal, current_principal, require_approver
from app.core.config import get_settings
from app.db.base import get_session
from app.db.models import AgentRun, AppSetting
from app.domain.models import IncidentAnalysis
from app.observability import recording
from app.services import run_store
from app.services.cache import build_cache
from app.services.prometheus import PrometheusClient, PrometheusMonitoringProvider
from app.services.settings_store import get_model_selection

router = APIRouter(prefix="/runs", tags=["runs"])

#: Компилированный граф один раз на процесс. Явный тип нужен, чтобы слой API
#: видел ainvoke и не терял проверку типов на главном пути запуска.
RunGraph = CompiledStateGraph[AgentState, Any, Any, Any]


@lru_cache(maxsize=8)
def get_graph(model_name: str | None = None) -> RunGraph:
    """Компилируется один раз на процесс.

    Граф не хранит состояние конкретного запуска — узлы читают и возвращают
    состояние, а исполнитель инструментов пересобирается из состояния при каждом
    вызове — поэтому один скомпилированный граф безопасно обслуживает параллельные
    запросы. Компиляция на каждый запрос означала бы также повторное чтение
    учётных данных и пересборку реестра при каждом расследовании.
    """
    settings = get_settings()
    runtime_settings = settings.model_copy(update={"llm_model": model_name or settings.llm_model})
    return build_graph(
        model=build_chat_model(runtime_settings),
        checkpointer=get_saver(),
        cache=build_cache(settings),
        use_llm=runtime_settings.llm_enabled,
    )


async def _real_graph(
    session: AsyncSession, model_name: str | None
) -> tuple[RunGraph, Callable[[], Awaitable[None]]] | None:
    """Собрать граф поверх реальных источников только для чтения, когда есть минимум.

    Реальные источники остаются изолированными от demo-провайдеров. Отсутствующий
    необязательный источник виден в трассе и не переключает расследование на
    синтетические данные незаметно.
    """
    settings = get_settings()
    selected = await session.get(AppSetting, SELECTED_KEY)
    if selected is None or not settings.prometheus_url:
        return None
    value = selected.value
    installation_id = value.get("installation_id")
    repository_id = value.get("id")
    repository = value.get("full_name")
    if (
        not isinstance(installation_id, int)
        or not isinstance(repository_id, int)
        or not isinstance(repository, str)
    ):
        return None
    connector = _connector_for(settings)
    allowed = await connector.repositories(installation_id)
    if not any(item["id"] == repository_id and item["full_name"] == repository for item in allowed):
        return None
    prometheus = PrometheusClient(
        settings.prometheus_url, service_label=settings.prometheus_service_label
    )
    runtime_settings = settings.model_copy(update={"llm_model": model_name or settings.llm_model})
    graph = build_graph(
        monitoring=PrometheusMonitoringProvider(prometheus),
        code=GitHubCodeProvider(connector, repository),
        logs=NoLogProvider(),
        issues=GitHubIssueProvider(connector, repository),
        knowledge=LocalRunbookProvider(settings.runbooks_dir),
        model=build_chat_model(runtime_settings),
        checkpointer=get_saver(),
        cache=build_cache(settings),
        use_llm=runtime_settings.llm_enabled,
    )
    return graph, prometheus.close


async def _graph_for_run(
    session: AsyncSession, model_name: str | None
) -> tuple[RunGraph, Callable[[], Awaitable[None]] | None]:
    real = await _real_graph(session, model_name)
    return real if real is not None else (get_graph(model_name), None)


def _to_detail(run: AgentRun) -> RunDetail:
    detail = RunDetail.model_validate(run)
    if run.analyses:
        detail.analysis = IncidentAnalysis.model_validate(run.analyses[-1].payload)
    detail.tool_calls = [
        ToolCallView.model_validate(tc) for tc in sorted(run.tool_calls, key=lambda t: t.started_at)
    ]
    # API возвращает запись подтверждения, а не состояние графа: именно она
    # разрешает действие и живёт дольше чекпоинта.
    pending = next((a for a in run.approvals if a.state is ApprovalState.PENDING), None)
    if pending is not None:
        detail.pending_approval = PendingApproval(
            approval_id=pending.id,
            tool=pending.tool,
            arguments=pending.arguments,
            rationale=pending.rationale,
        )

    decided = next((a for a in run.approvals if a.decided_at is not None), None)
    if decided is not None:
        detail.approved_by = decided.decided_by
        detail.action_result = decided.execution_result
    return detail


def _interrupt_payload(final: Mapping[str, Any]) -> dict[str, Any] | None:
    """Чего ждёт граф, если он приостановлен.

    LangGraph сообщает о паузе, кладя полезную нагрузку прерывания в возвращённое
    состояние, а не выбрасывая исключение, поэтому вызывающий, игнорирующий этот
    ключ, молча считает незавершённый запуск завершённым.
    """
    interrupts = final.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


@router.post("", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
async def start_run(
    payload: RunRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> RunDetail:
    """Запустить расследование и вернуть его терминальное состояние.

    Запуск выполняется синхронно: расследование против mock- или внутрипроцессных
    провайдеров завершается заметно меньше чем за секунду, а синхронный ответ
    честно показывает, сколько времени это на самом деле занимает.

    «Терминальное» включает приостановленное. Если агент предложил запись, граф
    останавливается на шлюзе подтверждения, и это возвращает запуск со статусом
    awaiting_approval и точным содержимым, ожидающим проверки; решение
    приходит отдельным запросом POST /runs/{id}/approval.
    """
    model_selection = await get_model_selection(session, get_settings())
    run = await run_store.create_run(
        session,
        task=payload.task,
        target_service=payload.target_service,
        model_name=model_selection.model_name,
        actor=principal.actor,
    )
    recording.record_run_started(payload.target_service)

    started = time.perf_counter()
    graph, close_graph = await _graph_for_run(session, run.model_name)
    try:
        final = cast(
            AgentState,
            await graph.ainvoke(
                initial_state(str(run.id), payload.task, payload.target_service),
                run_config(str(run.id)),
            ),
        )
    finally:
        if close_graph is not None:
            await close_graph()
    await run_store.persist_progress(session, run, final, pending=_interrupt_payload(final))
    recording.record_run(final, duration_seconds=time.perf_counter() - started)
    stored = await run_store.get_run(session, run.id)
    assert stored is not None
    return _to_detail(stored)


@router.post("/{run_id}/approval", response_model=RunDetail)
async def decide_approval(
    run_id: uuid.UUID,
    decision: ApprovalDecision,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_approver),
) -> RunDetail:
    """Подтвердить или отклонить запись, которой ждёт запуск, и возобновить его.

    Решение не несёт само действие. Выполняется то, что граф сохранил в чекпоинт
    при паузе, поэтому подтверждение нельзя перенаправить на другое содержимое,
    нежели увидел проверяющий — этот запрос говорит «да» или «нет», и не более.
    """
    run = await run_store.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Запуск не найден.")
    if run.status is not RunStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Запуск в состоянии {run.status}, а не ждёт подтверждения.",
        )

    await run_store.record_decision(
        session,
        run,
        approved=decision.approved,
        decided_by=principal.actor,
        note=decision.note,
    )
    recording.record_decision(approved=decision.approved)

    started = time.perf_counter()
    graph, close_graph = await _graph_for_run(session, run.model_name)
    try:
        final = cast(
            AgentState,
            await graph.ainvoke(
                Command(
                    resume={
                        "approved": decision.approved,
                        "decided_by": principal.actor,
                        "note": decision.note,
                    }
                ),
                run_config(str(run.id)),
            ),
        )
    finally:
        if close_graph is not None:
            await close_graph()
    await run_store.persist_progress(session, run, final, pending=_interrupt_payload(final))
    recording.record_run(final, duration_seconds=time.perf_counter() - started)
    stored = await run_store.get_run(session, run.id)
    assert stored is not None
    return _to_detail(stored)


@router.get("/{run_id}/trace", response_model=RunTrace)
async def get_trace(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> RunTrace:
    """Почему агент пришёл к тому, к чему пришёл.

    Восстанавливается из наблюдений, которые каждый узел добавлял по ходу работы,
    поэтому показывает посещённые узлы, вызванные инструменты и пройденные ветви —
    без повторного запуска. Рассуждения модели намеренно отсутствуют: то, что
    агент сделал, проверяемо, а то, что он «думал», не является доказательством.
    """
    run = await run_store.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Запуск не найден.")

    snapshot = run.state_snapshot or {}
    entries = [
        TraceStep(
            step=index,
            node=str(observation.get("node", observation.get("tool", "unknown"))),
            detail={k: v for k, v in observation.items() if k != "node"},
        )
        for index, observation in enumerate(snapshot.get("observations", []), start=1)
    ]
    return RunTrace(
        run_id=run.id,
        status=run.status,
        steps=entries,
        tool_calls=[
            ToolCallView.model_validate(tc)
            for tc in sorted(run.tool_calls, key=lambda t: t.started_at)
        ],
        errors=[str(e.get("message", "")) for e in snapshot.get("errors", [])],
    )


@router.get("", response_model=list[RunSummary])
async def list_runs(
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> list[RunSummary]:
    runs = await run_store.list_runs(session, limit=min(limit, 200))
    return [RunSummary.model_validate(r) for r in runs]


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> RunDetail:
    run = await run_store.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Запуск не найден.")
    return _to_detail(run)
