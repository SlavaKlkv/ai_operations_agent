"""Agent run endpoints."""

from __future__ import annotations

import time
import uuid
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.checkpointing import get_saver
from app.agent.graph import build_graph, run_config
from app.agent.state import ApprovalState, RunStatus, initial_state
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
from app.db.models import AgentRun
from app.domain.models import IncidentAnalysis
from app.observability import recording
from app.services import run_store
from app.services.cache import build_cache

router = APIRouter(prefix="/runs", tags=["runs"])


@lru_cache(maxsize=1)
def get_graph():
    """Compile once per process.

    The graph holds no per-run state — nodes read and return state, and the
    tool executor is rebuilt from state on every call — so one compiled graph
    serves concurrent requests safely. Compiling per request would also mean
    re-reading credentials and rebuilding the registry on every investigation.
    """
    settings = get_settings()
    return build_graph(
        checkpointer=get_saver(),
        cache=build_cache(settings),
    )


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


def _interrupt_payload(final: dict) -> dict | None:
    """What the graph is waiting for, if it paused.

    LangGraph reports a pause by putting the interrupt payload on the returned
    state rather than by raising, so a caller that ignores this key silently
    treats a half-finished run as a finished one.
    """
    interrupts = final.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


@router.post("", response_model=RunDetail, status_code=status.HTTP_201_CREATED)
async def start_run(
    payload: RunRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(current_principal),
) -> RunDetail:
    """Start an investigation and return its terminal state.

    The run executes inline: an investigation against mock or in-process
    providers finishes in well under a second, and a synchronous answer keeps
    the API honest about how long one actually takes.

    "Terminal" includes *paused*. If the agent proposed a write, the graph
    stops at the approval gate and this returns a run whose status is
    ``awaiting_approval`` with the exact content awaiting review; the decision
    arrives as a separate request to ``POST /runs/{id}/approval``.
    """
    run = await run_store.create_run(
        session,
        task=payload.task,
        target_service=payload.target_service,
        actor=principal.actor,
    )
    recording.record_run_started(payload.target_service)

    started = time.perf_counter()
    final = await get_graph().ainvoke(
        initial_state(str(run.id), payload.task, payload.target_service),
        run_config(str(run.id)),
    )
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
    """Approve or reject the write the run is waiting on, and resume it.

    The decision does not carry the action. What gets executed is what the
    graph checkpointed when it paused, so an approval cannot be redirected
    onto different content than the reviewer was shown — this request says
    yes or no, and nothing more.
    """
    run = await run_store.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    if run.status is not RunStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"run is {run.status}, not awaiting approval"
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
    final = await get_graph().ainvoke(
        Command(
            resume={
                "approved": decision.approved,
                "decided_by": principal.actor,
                "note": decision.note,
            }
        ),
        run_config(str(run.id)),
    )
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
    """Why the agent arrived where it did.

    Reconstructed from the observations every node appended as it ran, so it
    shows the nodes visited, the tools called and the branches taken —
    without re-running anything. Model reasoning is deliberately absent: what
    the agent did is auditable, what it "thought" is not evidence.
    """
    run = await run_store.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")

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
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    return _to_detail(run)
