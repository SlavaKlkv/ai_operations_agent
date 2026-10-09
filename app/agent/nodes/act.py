"""Предложение записи, пауза ради человека и исполнение одобренного.

Это три отдельных узла, потому что это три отдельных события в журнале аудита:
что агент хотел сделать, что решил человек и что фактически произошло. Их
слияние сделало бы невозможным ответ на вопрос, ради которого существует эта
часть системы, — кто авторизовал это изменение.

Пауза настоящая. interrupt останавливает граф, а чекпоинтер сохраняет
состояние, поэтому подтверждение — это не переменная в памяти, пока корутина
ждёт: процесс может перезапуститься между предложением и решением, и запуск
возобновится с того места, где остановился.

Две независимые вещи должны быть истинны, прежде чем запись выполнится.
Человек должен одобрить, и одобренное содержимое должно быть тем содержимым,
которое показали — execute_action перечитывает предложение из состояния, а
не доверяет тому, что приходит с возобновлением, поэтому подтверждение нельзя
перенаправить на другое действие.
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
    """Превратить анализ в конкретную, проверяемую запись — или отказаться.

    Текст issue отрисовывается из анализа кодом, а не пишется моделью. Вердикт
    и уверенность анализа вычисляются из детерминированных гипотез, а его
    доказательства пришли из вызовов инструментов, поэтому отрисовка здесь
    значит, что текст, одобряемый человеком, не может содержать утверждение,
    которого нет в состоянии, доступном для проверки.
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
            f"Расследование выявило вероятную причину с уверенностью "
            f"{analysis.confidence:.2f}; заведение задачи сохранит доказательства "
            f"для того, кто займётся сервисом {analysis.service}."
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
    """Человек нужен только для записи; исследование только для чтения просто отчитывается."""
    actions = state.get("proposed_actions") or []
    if any(a.requires_approval for a in actions):
        return "request_approval"
    return "final_response"


# ── request_approval ─────────────────────────────────────────────────────────


async def request_approval_node(state: AgentState) -> AgentState:
    """Остановить граф и ждать человека.

    interrupt выбрасывает исключение из узла; чекпоинтер записывает
    состояние, и вызывающий видит нагрузку ниже. Возобновление через
    Command(resume=…) перезапускает этот узел с начала, и во второй раз
    interrupt возвращает решение вместо паузы.
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
    """Отказ — это нормальное завершение, а не сбой."""
    if state.get("approval_state") is ApprovalState.APPROVED:
        return "execute_action"
    return "final_response"


# ── execute_action ───────────────────────────────────────────────────────────


def make_execute_action_node(registry: ToolRegistry, guardrails: Guardrails):
    """Выполнить одобренную запись один раз под политикой, расширенной только для этого шага."""

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
                    summary=f"действие выполнено: {invocation.digest}",
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


# ── Отрисовка ────────────────────────────────────────────────────────────────


def issue_title(analysis: IncidentAnalysis) -> str:
    started = f" с {analysis.incident_start:%H:%M} UTC" if analysis.incident_start else ""
    return f"Повышенные ошибки в {analysis.service}{started}"[:200]


def issue_body(analysis: IncidentAnalysis, state: AgentState) -> str:
    """Текст issue, отрисованный из анализа.

    Написан как отчёт, который хотел бы получить дежурный инженер: что
    произошло, каковы доказательства, что подозревается и что делать — с
    каждым утверждением, отслеживаемым до породившего его инструмента.
    """
    lines = ["## Итог", analysis.summary or "Сводка не была построена.", ""]

    if analysis.suspected_causes:
        lines.append("## Подозреваемая причина")
        lines += [
            f"- **уверенность {h.confidence:.0%}** — {h.statement}"
            for h in analysis.suspected_causes
        ]
        lines.append("")

    if analysis.symptoms:
        lines.append("## Симптомы")
        lines += [f"- {s}" for s in analysis.symptoms]
        lines.append("")

    if analysis.evidence:
        lines.append("## Доказательства")
        lines += [f"- `{e.source_tool}` — {e.summary}" for e in analysis.evidence]
        lines.append("")

    if analysis.recommended_actions:
        lines.append("## Рекомендуемые действия")
        lines += [f"{n}. {a}" for n, a in enumerate(analysis.recommended_actions, 1)]
        lines.append("")

    failures = [e.message for e in state.get("errors", []) if e.recoverable]
    if failures:
        lines.append("## Данные, которые не удалось собрать")
        lines += [f"- {m}" for m in failures]
        lines.append("")

    lines += [
        "---",
        (
            f"Заведено AI Operations Agent по запуску `{state.get('run_id')}` после "
            f"{state.get('tool_call_count', 0)} вызовов инструментов. Проверено и одобрено "
            "человеком перед созданием."
        ),
    ]
    return "\n".join(lines)[:MAX_BODY_CHARS]


def _field(decision: Any, name: str, *, default: Any) -> Any:
    """Прочитать одно поле из того, чем оказалось значение возобновления.

    Возобновление может быть отображением, объектом или голым True от
    вызывающего, который просто хотел сказать «да». Нормализация здесь
    избавляет узел от заботы об этом.
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
