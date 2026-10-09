"""Превращение завершённого запуска в метрики и читаемую трассу.

Намеренно отделён от графа. Узлы возвращают состояние; этот модуль читает это
состояние и решает, что стоит считать. Альтернатива — разбросать counter.inc()
по узлам — делает рабочий процесс менее читаемым и привязывает логику агента к
тому бэкенду метрик, что сейчас в моде.

Трасса — это ответ на вопрос «почему агент пришёл к такому выводу». Она фиксирует
наблюдаемые решения: какой узел выполнился, какой инструмент был вызван с какими
аргументами, что вернулось в сводке и куда рабочий процесс ветвился. Она не
фиксирует рассуждения модели, потому что система от них не зависит — то, что
агент сделал, проверяемо, а то, что он «думал», не является доказательством.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from app.agent.state import AgentState, ApprovalState, RunStatus
from app.observability import metrics

log = structlog.get_logger(__name__)

WRITE_TOOLS = frozenset({"create_issue", "add_issue_comment"})


def record_run(state: AgentState, *, duration_seconds: float) -> None:
    """Учесть одно завершённое или приостановленное расследование."""
    service = state.get("target_service") or "unknown"
    status = state.get("status", RunStatus.RUNNING)

    metrics.runs_finished.labels(service=service, status=str(status)).inc()
    metrics.run_duration.labels(service=service).observe(duration_seconds)
    metrics.run_tool_calls.observe(state.get("tool_call_count", 0))

    analysis = state.get("analysis")
    if analysis is not None:
        metrics.run_confidence.observe(analysis.confidence)

    for call in state.get("tool_calls", []):
        metrics.tool_calls.labels(tool=call.tool, outcome="ok" if call.ok else "error").inc()
        metrics.tool_duration.labels(tool=call.tool).observe(call.duration_ms / 1000)

    _record_model_usage(state)
    _record_write_safety(state)


def record_run_started(service: str | None) -> None:
    metrics.runs_started.labels(service=service or "unknown").inc()


def record_decision(*, approved: bool) -> None:
    metrics.approvals.labels(decision="approved" if approved else "rejected").inc()


def _record_model_usage(state: AgentState) -> None:
    calls = state.get("llm_calls", 0)
    failures = sum(1 for e in state.get("errors", []) if e.kind in ("planner_failed", "llm_failed"))
    if calls:
        metrics.llm_calls.labels(outcome="ok").inc(calls)
    if failures:
        metrics.llm_calls.labels(outcome="error").inc(failures)

    if tokens := state.get("input_tokens", 0):
        metrics.llm_tokens.labels(direction="input").inc(tokens)
    if tokens := state.get("output_tokens", 0):
        metrics.llm_tokens.labels(direction="output").inc(tokens)


def _record_write_safety(state: AgentState) -> None:
    """Единственный счётчик, который никогда не должен сдвинуться.

    Проверяется по записанным фактам, а не берётся на веру из флага: если запись
    появляется в журнале вызовов, а запуск не несёт подтверждения, шлюз отказал,
    и метрика должна сказать об этом достаточно громко, чтобы кого-то разбудить.
    """
    if state.get("approval_state") is ApprovalState.APPROVED:
        return
    for call in state.get("tool_calls", []):
        if call.tool in WRITE_TOOLS and call.ok:
            metrics.unapproved_writes.labels(tool=call.tool).inc()
            log.error(
                "agent.unapproved_write",
                run_id=state.get("run_id"),
                tool=call.tool,
                approval_state=str(state.get("approval_state")),
            )


def record_integration_health(statuses, *, durable_checkpointer: bool) -> None:
    for status in statuses:
        metrics.mcp_server_up.labels(server=status.name, required=str(status.required).lower()).set(
            1 if status.connected else 0
        )
    metrics.checkpointer_durable.set(1 if durable_checkpointer else 0)


# ── Трасса ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """Одно наблюдаемое действие рабочего процесса."""

    step: int
    node: str
    detail: dict[str, Any]


def build_trace(state: AgentState) -> list[TraceEntry]:
    """Восстановить запуск из его наблюдений, по порядку.

    Наблюдения добавляются каждым узлом по ходу работы, поэтому их воспроизведение
    — это и есть исполнение графа: посещённые узлы, вызванные инструменты,
    пройденные ветви. Именно это делает вопрос «почему агент пришёл сюда»
    отвечаемым задним числом без повторного запуска.
    """
    return [
        TraceEntry(
            step=index,
            node=str(observation.get("node", observation.get("tool", "?"))),
            detail={k: v for k, v in observation.items() if k != "node"},
        )
        for index, observation in enumerate(state.get("observations", []), start=1)
    ]


def render_trace(state: AgentState) -> str:
    """Трасса в виде текста, для терминала или комментария к issue."""
    lines = [f"run {state.get('run_id')} — {state.get('status')}"]
    for entry in build_trace(state):
        summary = ", ".join(f"{k}={_short(v)}" for k, v in entry.detail.items() if v is not None)
        lines.append(f"  {entry.step:>2}. {entry.node}: {summary}")
    if analysis := state.get("analysis"):
        lines.append(f"  → {analysis.confidence:.2f} {analysis.summary}")
    return "\n".join(lines)


def _short(value: Any, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
