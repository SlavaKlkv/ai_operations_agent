"""Превратить произвольный запрос в ограниченный, типизированный план расследования.

V1 делает это детерминированно: регулярное выражение по известным именам
сервисов плюс явные значения по умолчанию. Детерминированный код
предпочитается всюду, где ответ фактически не требует понимания языка, —
вариант на основе LLM появится в V2 и должен давать ту же форму
TaskAnalysis.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.adapters.mock.dataset import DEFAULT_SCENARIO
from app.agent.state import AgentState

_SERVICE_RE = re.compile(r"\b([a-z0-9]+(?:-[a-z0-9]+)*-service)\b", re.IGNORECASE)
_KEYWORD_RE = re.compile(r"\b(5xx|4xx|latency|errors?|timeout)\b", re.IGNORECASE)
DEFAULT_LOOKBACK = timedelta(hours=1)


class TaskAnalysis(BaseModel):
    """Структурированное прочтение запроса пользователя."""

    model_config = ConfigDict(extra="forbid")

    target_service: str | None = None
    window_start: datetime
    window_end: datetime
    keywords: list[str] = Field(default_factory=list)


def extract_service(task: str) -> str | None:
    match = _SERVICE_RE.search(task)
    return match.group(1).lower() if match else None


def analyse_task(task: str, now: datetime | None = None) -> TaskAnalysis:
    reference = now or _scenario_now()
    return TaskAnalysis(
        target_service=extract_service(task),
        window_start=reference - DEFAULT_LOOKBACK,
        window_end=reference,
        keywords=sorted({w.lower() for w in _KEYWORD_RE.findall(task)}),
    )


def _scenario_now() -> datetime:
    """Привязать окно к синтетическому миру, пока настоящих часов нет."""
    latest = max(
        (p.timestamp for series in DEFAULT_SCENARIO.metrics.values() for p in series.points),
        default=datetime.now(UTC),
    )
    return latest


async def analyze_task_node(state: AgentState) -> AgentState:
    analysis = analyse_task(state["task"])
    service = state.get("target_service") or analysis.target_service
    return AgentState(
        target_service=service,
        window_start=analysis.window_start,
        window_end=analysis.window_end,
        current_step="analyze_task",
        step_count=state.get("step_count", 0) + 1,
        observations=[
            {
                "node": "analyze_task",
                "target_service": service,
                "window": [analysis.window_start.isoformat(), analysis.window_end.isoformat()],
                "keywords": analysis.keywords,
            }
        ],
    )
