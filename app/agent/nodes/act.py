"""Proposing a write, pausing for a human, and executing what was approved.

The three are separate nodes because they are separate events in the audit
trail: what the agent wanted to do, what a person decided, and what actually
happened. Collapsing them would make it impossible to answer the question this
part of the system exists to answer — *who* authorised this change.

The pause is a real one. ``interrupt`` stops the graph and the checkpointer
persists the state, so approval is not a variable held in memory while a
coroutine waits: the process can restart between the proposal and the
decision, and the run resumes from where it stopped.

Two independent things must both be true before a write runs. A human must
have approved, and the approved content must be the content that was shown —
``execute_action`` re-reads the proposal from state rather than trusting
anything that arrives with the resume, so an approval cannot be redirected
onto a different action.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from langgraph.types import interrupt

from app.agent.guardrails import Guardrails
from app.agent.state import (
    AgentState,
    ApprovalState,
    ProposedAction,
    RunError,
    RunStatus,
)
from app.agent.tools.base import ToolRegistry, ToolRequest
from app.agent.tools.executor import ToolExecutor
from app.domain.models import Evidence, EvidenceKind, IncidentAnalysis

log = structlog.get_logger(__name__)

#: Ниже этого порога агент только сообщает о находках и ничего не предлагает.
#: задача, созданная по догадке, вынуждает кого-то проводить опровержение.
PROPOSAL_CONFIDENCE_FLOOR = 0.6

MAX_BODY_CHARS = 20_000


# ── propose_action ───────────────────────────────────────────────────────────


async def propose_action_node(state: AgentState) -> AgentState:
    """Turn the analysis into a concrete, reviewable write — or decline to.

    The issue body is rendered from the analysis by code, not written by the
    model. The analysis verdict and confidence are computed from deterministic
    hypotheses, and its evidence came from tool calls, so rendering it here
    means the text a human approves cannot contain a claim that is not in the
    state they can inspect.
    """
    analysis = state.get("analysis")
    step = state.get("step_count", 0) + 1

    if analysis is None:
        return AgentState(
            current_step="propose_action",
            step_count=step,
            proposed_actions=[],
            approval_state=ApprovalState.NOT_REQUIRED,
        )

    hypotheses = list(state.get("hypotheses", []))
    strongest_hypothesis = max(
        (hypothesis.confidence for hypothesis in hypotheses),
        default=0.0,
    )
    if (
        analysis.confidence < PROPOSAL_CONFIDENCE_FLOOR
        or strongest_hypothesis < PROPOSAL_CONFIDENCE_FLOOR
    ):
        log.info(
            "agent.no_proposal",
            run_id=state.get("run_id"),
            confidence=analysis.confidence,
            strongest_hypothesis=strongest_hypothesis,
        )
        return AgentState(
            current_step="propose_action",
            step_count=step,
            proposed_actions=[],
            approval_state=ApprovalState.NOT_REQUIRED,
            observations=[
                {
                    "node": "propose_action",
                    "proposed": None,
                    "reason": (
                        f"analysis confidence {analysis.confidence:.2f} and strongest "
                        f"deterministic hypothesis {strongest_hypothesis:.2f} must both "
                        f"reach the {PROPOSAL_CONFIDENCE_FLOOR} floor for proposing a write"
                    ),
                }
            ],
        )

    action = ProposedAction(
        tool="create_issue",
        arguments={
            "title": issue_title(analysis),
            "body": issue_body(analysis, state),
            "service": analysis.service,
            "labels": sorted({"incident", analysis.service}),
        },
        rationale=(
            f"The investigation identified a likely cause with confidence "
            f"{analysis.confidence:.2f}; filing it preserves the evidence for whoever "
            f"picks up {analysis.service}."
        ),
        requires_approval=True,
    )
    return AgentState(
        current_step="propose_action",
        step_count=step,
        proposed_actions=[action],
        approval_state=ApprovalState.PENDING,
        observations=[
            {"node": "propose_action", "proposed": action.tool, "title": action.arguments["title"]}
        ],
    )


def route_after_proposal(state: AgentState) -> Literal["request_approval", "final_response"]:
    """Only a write needs a human; a read-only investigation just reports."""
    actions = state.get("proposed_actions") or []
    if any(a.requires_approval for a in actions):
        return "request_approval"
    return "final_response"


# ── request_approval ─────────────────────────────────────────────────────────


async def request_approval_node(state: AgentState) -> AgentState:
    """Stop the graph and wait for a person.

    ``interrupt`` raises out of the node; the checkpointer writes the state,
    and the caller sees the payload below. Resuming with ``Command(resume=…)``
    re-runs this node from the top, and the second time ``interrupt`` returns
    the decision instead of pausing.
    """
    action = (state.get("proposed_actions") or [None])[0]
    if action is None:
        return AgentState(
            current_step="request_approval",
            step_count=state.get("step_count", 0) + 1,
            approval_state=ApprovalState.NOT_REQUIRED,
        )

    decision = interrupt(
        {
            "kind": "approval_required",
            "run_id": state.get("run_id"),
            "tool": action.tool,
            "arguments": action.arguments,
            "rationale": action.rationale,
        }
    )

    approved = bool(_field(decision, "approved", default=False))
    decided_by = str(_field(decision, "decided_by", default="") or "")
    note = str(_field(decision, "note", default="") or "")

    log.info(
        "agent.approval_decided",
        run_id=state.get("run_id"),
        approved=approved,
        decided_by=decided_by or "unknown",
    )
    return AgentState(
        current_step="request_approval",
        step_count=state.get("step_count", 0) + 1,
        approval_state=ApprovalState.APPROVED if approved else ApprovalState.REJECTED,
        approved_by=decided_by or None,
        approval_note=note,
        observations=[
            {
                "node": "request_approval",
                "approved": approved,
                "decided_by": decided_by or "unknown",
                "note": note,
            }
        ],
    )


def route_after_approval(state: AgentState) -> Literal["execute_action", "final_response"]:
    """A rejection is a normal ending, not a failure."""
    if state.get("approval_state") is ApprovalState.APPROVED:
        return "execute_action"
    return "final_response"


# ── execute_action ───────────────────────────────────────────────────────────


def make_execute_action_node(registry: ToolRegistry, guardrails: Guardrails):
    """Run the approved write, once, under a policy widened only for this step."""

    async def execute_action_node(state: AgentState) -> AgentState:
        step = state.get("step_count", 0) + 1
        action = (state.get("proposed_actions") or [None])[0]

        if state.get("approval_state") is not ApprovalState.APPROVED or action is None:
            # Эшелонированная защита: маршрутизация уже запрещает это, а исполнитель
            # всё равно откажет. Нужны три независимые проверки, потому что только
            # здесь система изменяет что-либо за своими пределами.
            return AgentState(
                current_step="execute_action",
                step_count=step,
                errors=[
                    RunError(
                        node="execute_action",
                        kind="not_approved",
                        message="reached the write step without an approved action",
                        recoverable=False,
                    )
                ],
            )

        # Разрешение выдаётся здесь только для этого шага и не видно другим узлам:
        # цикл расследования продолжает использовать исходную политику.
        approved_policy = guardrails.with_write_approved().narrowed_to({action.tool})
        executor = ToolExecutor.resume(registry, approved_policy, state.get("tool_calls", []))

        invocation = await executor.execute(
            ToolRequest(
                tool=action.tool,
                arguments=action.arguments,
                reason=f"approved by {state.get('approved_by') or 'a reviewer'}",
            )
        )

        errors = (
            []
            if invocation.ok
            else [
                RunError(
                    node="execute_action",
                    kind="write_failed",
                    message=f"{action.tool}: {invocation.error}",
                    recoverable=False,
                )
            ]
        )
        evidence = (
            [
                Evidence(
                    kind=EvidenceKind.DOCUMENT,
                    summary=f"action executed: {invocation.digest}",
                    source_tool=action.tool,
                    reference=action.tool,
                )
            ]
            if invocation.ok
            else []
        )
        log.info(
            "agent.action_executed",
            run_id=state.get("run_id"),
            tool=action.tool,
            ok=invocation.ok,
            error=invocation.error,
        )
        return AgentState(
            current_step="execute_action",
            step_count=step,
            tool_call_count=state.get("tool_call_count", 0) + 1,
            tool_calls=[invocation.record],
            evidence=evidence,
            errors=errors,
            action_result=_result_payload(invocation),
            status=RunStatus.RUNNING if invocation.ok else RunStatus.FAILED,
            observations=[
                {
                    "node": "execute_action",
                    "tool": action.tool,
                    "ok": invocation.ok,
                    "summary": invocation.digest or invocation.error,
                }
            ],
        )

    return execute_action_node


# ── Rendering ────────────────────────────────────────────────────────────────


def issue_title(analysis: IncidentAnalysis) -> str:
    started = f" from {analysis.incident_start:%H:%M} UTC" if analysis.incident_start else ""
    return f"Elevated errors in {analysis.service}{started}"[:200]


def issue_body(analysis: IncidentAnalysis, state: AgentState) -> str:
    """The issue text, rendered from the analysis.

    Written as the report an on-call engineer would want: what happened, what
    the evidence is, what is suspected, and what to do — with every claim
    traceable to the tool that produced it.
    """
    lines = ["## Summary", analysis.summary or "No summary was produced.", ""]

    if analysis.suspected_causes:
        lines.append("## Suspected cause")
        lines += [
            f"- **{h.confidence:.0%} confidence** — {h.statement}"
            for h in analysis.suspected_causes
        ]
        lines.append("")

    if analysis.symptoms:
        lines.append("## Symptoms")
        lines += [f"- {s}" for s in analysis.symptoms]
        lines.append("")

    if analysis.evidence:
        lines.append("## Evidence")
        lines += [f"- `{e.source_tool}` — {e.summary}" for e in analysis.evidence]
        lines.append("")

    if analysis.recommended_actions:
        lines.append("## Recommended actions")
        lines += [f"{n}. {a}" for n, a in enumerate(analysis.recommended_actions, 1)]
        lines.append("")

    failures = [e.message for e in state.get("errors", []) if e.recoverable]
    if failures:
        lines.append("## Data that could not be collected")
        lines += [f"- {m}" for m in failures]
        lines.append("")

    lines += [
        "---",
        (
            f"Filed by the AI Operations Agent from run `{state.get('run_id')}` after "
            f"{state.get('tool_call_count', 0)} tool calls. Reviewed and approved by a human "
            "before creation."
        ),
    ]
    return "\n".join(lines)[:MAX_BODY_CHARS]


def _field(decision: Any, name: str, *, default: Any) -> Any:
    """Read one field out of whatever the resume value happened to be.

    A resume can be a mapping, an object, or a bare ``True`` from a caller
    that just wanted to say yes. Normalising here keeps the node from caring.
    """
    if isinstance(decision, dict):
        return decision.get(name, default)
    if isinstance(decision, bool) and name == "approved":
        return decision
    return getattr(decision, name, default)


def _result_payload(invocation) -> dict[str, Any]:
    if invocation.result is None:
        return {"ok": False, "error": invocation.error}
    return {"ok": True, **invocation.result.model_dump(mode="json")}
