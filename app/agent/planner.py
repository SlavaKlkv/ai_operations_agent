"""Deciding what to do next — the one genuinely agentic decision in the system.

Everything else the graph does is fixed: which context to collect, how to
detect a spike, which deployment precedes it. The open question is what to
look at *after* the obvious has been collected, and that depends on what the
obvious turned up. That is the decision worth giving to a model.

Two planners implement the same protocol.

:class:`LLMPlanner` binds the allowed tool schemas to the model and reads back
native tool calls. It is shown the task, what has already been observed and
what remains in the budget — nothing else. It cannot invent a tool, because
the registry refuses names it does not know; it cannot invent arguments,
because the schema rejects them; and it cannot loop forever, because the
budget is checked before its answer is honoured.

:class:`HeuristicPlanner` answers the same question with a gap-filling rule
set. It runs when no model is configured, and it is what the LLM planner is
measured against: a plan that does not beat the heuristic is not worth an API
call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import LLMError, Usage, invoke
from app.agent.state import AgentState, CollectedContext
from app.agent.tools.base import AgentTool, ToolRequest

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are the planning step of an incident-investigation workflow for backend \
services. Deterministic code has already collected baseline monitoring, \
deployment and error data, and has already correlated the error spike with the \
deployments that preceded it. Your only job is to decide whether one more piece \
of evidence would change the conclusion.

Rules:
- Call a tool only when a specific, named gap in the evidence would be closed \
by it. "More context" is not a gap.
- Never call a tool to confirm something already stated in the evidence below.
- If the evidence already supports a conclusion, or if no available tool could \
close the remaining gap, reply with a single short sentence saying what is \
still unknown, and call nothing.
- You cannot cause any change to any system. Every tool available to you is \
read-only.
"""


@dataclass(frozen=True, slots=True)
class Plan:
    """What the planner decided, plus what it cost to decide it."""

    requests: tuple[ToolRequest, ...] = ()
    #: Причина остановки планировщика, когда он ничего не запросил.
    rationale: str = ""
    usage: Usage = field(default_factory=Usage)
    #: Устанавливается при сбое планировщика, когда вызывающей стороне нужен запасной путь.
    error: str | None = None

    @property
    def is_done(self) -> bool:
        return not self.requests


class Planner(Protocol):
    async def plan(self, state: AgentState, available: Sequence[AgentTool[Any, Any]]) -> Plan: ...


# ── Heuristic ────────────────────────────────────────────────────────────────


class HeuristicPlanner:
    """Close named evidence gaps in a fixed order, then stop.

    The order encodes what an engineer would actually do: confirm the symptom
    is real (alerts), understand its shape (latency), then read the code that
    shipped. Each rule fires at most once because its precondition stops
    holding as soon as the data arrives — which is also why this planner
    terminates without needing a budget to stop it.
    """

    async def plan(self, state: AgentState, available: Sequence[AgentTool[Any, Any]]) -> Plan:
        names = {tool.name for tool in available}
        service = state.get("target_service")
        context = state.get("context") or CollectedContext()
        if not service:
            return Plan(rationale="no target service was resolved, so nothing can be queried")

        # Правило срабатывает на пробел в контексте, но пустой ответ инструмента
        # оставляет этот пробел: в окне действительно могло не быть алертов или
        # коммитов. Без этой проверки планировщик повторял бы запрос на каждой
        # итерации до исчерпания бюджета — это самый дорогой тип ошибки агента.
        attempted = {call.tool for call in state.get("tool_calls", [])}

        for request, reason in self._candidates(service, context):
            if request.tool in names and request.tool not in attempted:
                return Plan(requests=(request,), rationale=reason)
        return Plan(
            rationale=(
                "every source that could close a gap has been queried; "
                "what is missing is absent, not unfetched"
            )
        )

    @staticmethod
    def _candidates(service: str, context: CollectedContext):
        if not context.alerts:
            yield (
                ToolRequest(
                    tool="get_recent_alerts",
                    arguments={"service": service},
                    reason="no alert was observed; confirm whether monitoring agreed",
                ),
                "no alerts had been collected yet",
            )
        if "latency_p99" not in context.metrics:
            yield (
                ToolRequest(
                    tool="get_service_metrics",
                    arguments={"service": service, "metric": "latency_p99"},
                    reason="latency shape distinguishes a slow dependency from a code fault",
                ),
                "tail latency had not been measured",
            )
        if context.error_groups and not context.commits:
            yield (
                ToolRequest(
                    tool="get_commits",
                    arguments={"service": service},
                    reason="errors are grounded but no code change has been read yet",
                ),
                "errors were observed but no commits had been read",
            )


# ── LLM ──────────────────────────────────────────────────────────────────────


class LLMPlanner:
    """Let the model choose the next tool, within the offered set."""

    def __init__(self, model: BaseChatModel, *, max_requests: int = 2) -> None:
        self._model = model
        #: План — это шаг, а не список покупок. Ограничение параллельных вызовов не даёт
        #: один ошибочный шаг не потратил весь бюджет.
        self._max_requests = max_requests

    async def plan(self, state: AgentState, available: Sequence[AgentTool[Any, Any]]) -> Plan:
        if not available:
            return Plan(rationale="no tools remain available to this run")

        bound = self._model.bind_tools([tool.json_schema() for tool in available])
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=render_briefing(state, available)),
        ]
        try:
            message, usage = await invoke(bound, messages)
        except LLMError as exc:
            log.warning("planner.llm_failed", error=str(exc))
            return Plan(error=str(exc), rationale="the planning model was unavailable")

        requests = tuple(
            ToolRequest(
                tool=call["name"],
                arguments=dict(call.get("args") or {}),
                reason="selected by the planning model",
            )
            for call in (message.tool_calls or [])
        )[: self._max_requests]

        return Plan(requests=requests, rationale=_text_of(message), usage=usage)


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts = [c.get("text", "") for c in content if isinstance(c, dict)]
    return " ".join(p for p in parts if p).strip()


# ── Briefing ─────────────────────────────────────────────────────────────────


def render_briefing(state: AgentState, available: Sequence[AgentTool[Any, Any]]) -> str:
    """Everything the planner is allowed to know, and nothing else.

    Assembled from state rather than from an accumulating message history: the
    model sees the current findings, not a transcript of how they were reached.
    That keeps the prompt bounded as the loop iterates, and keeps state — not
    the conversation — the single source of truth about the run.
    """
    evidence = state.get("evidence", [])
    hypotheses = state.get("hypotheses", [])
    calls = state.get("tool_calls", [])
    budget = state.get("tool_budget_remaining")

    lines = [
        f"Task: {state.get('task', '')}",
        f"Service under investigation: {state.get('target_service') or 'not identified'}",
    ]
    window_start, window_end = state.get("window_start"), state.get("window_end")
    if window_start and window_end:
        lines.append(f"Window: {window_start:%Y-%m-%d %H:%M} to {window_end:%H:%M} UTC")

    lines.append("\nEvidence collected so far:")
    lines += [f"- [{item.kind}] {item.summary}" for item in evidence] or ["- (none)"]

    if hypotheses:
        lines.append("\nWorking hypotheses:")
        lines += [f"- ({h.confidence:.2f}) {h.statement}" for h in hypotheses]

    if calls:
        lines.append("\nTools already called (do not repeat these):")
        lines += [
            f"- {c.tool}({_brief_args(c.arguments)}) → {'ok' if c.ok else c.error}" for c in calls
        ]

    if budget is not None:
        lines.append(f"\nTool calls remaining in this run: {budget}")

    lines.append("\nAvailable tools: " + ", ".join(t.name for t in available))
    return "\n".join(lines)


def _brief_args(arguments: dict[str, Any]) -> str:
    """Timestamps are noise in a prompt the model must not anchor on."""
    interesting = {k: v for k, v in arguments.items() if k not in ("start", "end")}
    return ", ".join(f"{k}={v}" for k, v in sorted(interesting.items()))
