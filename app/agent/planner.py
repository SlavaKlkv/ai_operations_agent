"""Решение, что делать дальше, — единственное по-настоящему агентное решение в системе.

Всё остальное, что делает граф, фиксировано: какой контекст собирать, как
обнаружить всплеск, какой деплой ему предшествует. Открытый вопрос — на что
посмотреть после того, как собрано очевидное, и это зависит от того, что
обнаружило очевидное. Именно это решение стоит отдавать модели.

Два планировщика реализуют один протокол.

LLMPlanner привязывает разрешённые схемы инструментов к модели и
читает обратно нативные вызовы инструментов. Ему показывают задачу, то, что
уже наблюдалось, и то, что осталось в бюджете, — и ничего больше. Он не может
изобрести инструмент, потому что реестр отказывает незнакомым именам; не может
изобрести аргументы, потому что схема их отвергает; и не может зациклиться,
потому что бюджет проверяется до того, как его ответ принят.

HeuristicPlanner отвечает на тот же вопрос набором правил
заполнения пробелов. Он работает, когда модель не настроена, и именно с ним
сравнивают LLM-планировщик: план, не обыгрывающий эвристику, не стоит вызова
API.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
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
    """Что решил планировщик плюс во что обошлось это решение."""

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


# ── Эвристика ────────────────────────────────────────────────────────────────


class HeuristicPlanner:
    """Закрыть названные пробелы в доказательствах в заданном порядке и остановиться.

    Порядок кодирует то, что инженер действительно сделал бы: подтвердить, что
    симптом реален (оповещения), понять его форму (задержка), затем прочитать
    отгруженный код. Каждое правило срабатывает не более одного раза, потому
    что его предусловие перестаёт выполняться, как только приходят данные, —
    поэтому этот планировщик и завершается без бюджета, его останавливающего.
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
    def _candidates(service: str, context: CollectedContext) -> Iterator[tuple[ToolRequest, str]]:
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
        if context.error_groups:
            query = " ".join([service, *[group.error_type for group in context.error_groups[:2]]])
            yield (
                ToolRequest(
                    tool="search_runbooks",
                    arguments={"query": query, "service": service},
                    reason=(
                        "documented mitigation may reduce impact while the cause is investigated"
                    ),
                ),
                "observed errors have not been checked against local runbooks",
            )


# ── LLM ──────────────────────────────────────────────────────────────────────


class LLMPlanner:
    """Позволить модели выбрать следующий инструмент в пределах предложенного набора."""

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


# ── Сводка ───────────────────────────────────────────────────────────────────


def render_briefing(state: AgentState, available: Sequence[AgentTool[Any, Any]]) -> str:
    """Всё, что планировщику разрешено знать, и ничего больше.

    Собирается из состояния, а не из накапливаемой истории сообщений: модель
    видит текущие находки, а не расшифровку того, как к ним пришли. Это
    удерживает промпт ограниченным по мере итераций цикла и оставляет
    состояние — а не разговор — единственным источником правды о запуске.
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
    """Метки времени — шум в промпте, к которому модель не должна привязываться."""
    interesting = {k: v for k, v in arguments.items() if k not in ("start", "end")}
    return ", ".join(f"{k}={v}" for k, v in sorted(interesting.items()))
