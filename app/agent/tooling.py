"""Обёртка исполнения, через которую проходит каждый вызов инструмента в графе.

Она существует, чтобы таймауты, повторы и запись аудита были свойствами
среды исполнения, а не отдельных инструментов. Инструмент, забывший
обработать таймаут, всё равно ограничен; инструмент, завершившийся успешно,
всё равно записан.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.agent.state import ToolCallRecord


class ToolBudgetExceeded(RuntimeError):
    """Возбуждается, когда запуск пытается превысить разрешённое число вызовов инструментов."""


@dataclass(slots=True)
class ToolOutcome[T]:
    value: T | None
    record: ToolCallRecord

    @property
    def ok(self) -> bool:
        return self.record.ok


async def call_tool[T](
    name: str,
    fn: Callable[[], Awaitable[T]],
    *,
    arguments: dict[str, Any] | None = None,
    timeout: float = 15.0,
    retries: int = 1,
    summarise: Callable[[T], str] | None = None,
) -> ToolOutcome[T]:
    """Запустить fn с таймаутом, повторяя временные сбои.

    retries считает дополнительные попытки после первой. Возвращаемая
    запись всегда описывает последнюю попытку, а attempt говорит, сколько
    их понадобилось, — оценка использует это, чтобы выявить нестабильные или
    неправильно используемые инструменты.
    """
    arguments = arguments or {}
    last_error: str | None = None
    started = datetime.now(UTC)

    for attempt in range(1, retries + 2):
        t0 = time.perf_counter()
        try:
            value = await asyncio.wait_for(fn(), timeout=timeout)
        except TimeoutError:
            last_error = f"timeout after {timeout}s"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            return ToolOutcome(
                value=value,
                record=ToolCallRecord(
                    tool=name,
                    arguments=arguments,
                    started_at=started,
                    duration_ms=round((time.perf_counter() - t0) * 1000, 3),
                    ok=True,
                    result_summary=summarise(value) if summarise else "",
                    attempt=attempt,
                ),
            )

    return ToolOutcome(
        value=None,
        record=ToolCallRecord(
            tool=name,
            arguments=arguments,
            started_at=started,
            duration_ms=round((time.perf_counter() - t0) * 1000, 3),
            ok=False,
            error=last_error,
            attempt=retries + 1,
        ),
    )
