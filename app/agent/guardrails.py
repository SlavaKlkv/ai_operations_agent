"""Пределы, из которых запуск агента не может выговориться.

Каждое ограничение здесь обеспечивается в Python, до запуска инструмента, на
аргументах, которые предоставила модель. Ничто из этого не выражается
инструкцией в промпте: промпт — это просьба, и весь смысл защитного ограничения
в том, что оно не просьба.

Проверки упорядочены от самых дешёвых, и каждая возбуждает отдельное
исключение, поэтому граф может реагировать по-разному на «тебе нельзя этого
делать» (терминально) и «ты делал это достаточно раз» (остановиться и
подвести итог).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agent.tools.base import (
    AgentTool,
    ToolAccess,
    ToolNotAllowedError,
    ToolRegistry,
)


class GuardrailViolation(RuntimeError):
    """Запуск попытался сделать то, что запрещает его политика."""


class BudgetExhausted(GuardrailViolation):
    """Запуск исчерпал свой лимит вызовов инструментов или шагов рабочего процесса."""


class WriteNotApproved(GuardrailViolation):
    """До инструмента записи дошли без одобренного решения человека."""


class RepetitionLimitExceeded(GuardrailViolation):
    """Один и тот же инструмент вызывался с теми же аргументами слишком много раз.

    Циклы, повторно забирающие одни и те же данные, — характерный сбой агента,
    вызывающего инструменты, у которого кончились идеи. Обнаружение этого как
    нарушения политики превращает дорогой бесконечный цикл в дешёвое конечное
    состояние.
    """


@dataclass(frozen=True, slots=True)
class Guardrails:
    """Политика на запуск. Строится из настроек, никогда — из вывода модели."""

    max_tool_calls: int = 12
    max_workflow_steps: int = 30
    tool_timeout_seconds: float = 15.0
    #: Дополнительные попытки после первого неудачного вызова инструмента.
    tool_retries: int = 1
    #: None означает все инструменты реестра; множество сужает список.
    allowlist: frozenset[str] | None = None
    #: Инструменты записи недоступны до появления разрешающей записи подтверждения.
    allow_write: bool = False
    #: Сколько раз можно повторить одинаковый вызов до отказа.
    max_identical_calls: int = 2
    #: Инструменты, запрещённые в этом развёртывании независимо от других разрешений.
    denylist: frozenset[str] = field(default_factory=frozenset)

    def permits(self, tool: AgentTool[Any, Any]) -> bool:
        if tool.name in self.denylist:
            return False
        if self.allowlist is not None and tool.name not in self.allowlist:
            return False
        return self.allow_write or not tool.is_write

    def available(self, registry: ToolRegistry) -> tuple[AgentTool[Any, Any], ...]:
        """Инструменты, которые модели разрешено видеть. Больше ей никогда не показывают."""
        return tuple(t for t in registry if self.permits(t))

    def check_budget(self, *, tool_calls: int, steps: int) -> None:
        if tool_calls >= self.max_tool_calls:
            raise BudgetExhausted(f"tool-call budget spent ({tool_calls}/{self.max_tool_calls})")
        if steps >= self.max_workflow_steps:
            raise BudgetExhausted(f"workflow step budget spent ({steps}/{self.max_workflow_steps})")

    def check_tool(self, tool: AgentTool[Any, Any]) -> None:
        if tool.name in self.denylist:
            raise ToolNotAllowedError(f"{tool.name} is denied in this deployment")
        if self.allowlist is not None and tool.name not in self.allowlist:
            raise ToolNotAllowedError(f"{tool.name} is not in this run's allowlist")
        if tool.is_write and not self.allow_write:
            raise WriteNotApproved(
                f"{tool.name} is a {ToolAccess.WRITE} tool and needs an approved decision"
            )

    def check_repetition(self, signature: str, previous: Sequence[str]) -> None:
        seen = sum(1 for s in previous if s == signature)
        if seen >= self.max_identical_calls:
            raise RepetitionLimitExceeded(
                f"identical call repeated {seen} times; refusing to repeat it again"
            )

    def with_write_approved(self) -> Guardrails:
        """Политика для единственного шага, исполняющего одобренное действие."""
        return Guardrails(**{**_as_dict(self), "allow_write": True})

    def narrowed_to(self, names: Iterable[str]) -> Guardrails:
        wanted = frozenset(names)
        current = self.allowlist
        return Guardrails(
            **{**_as_dict(self), "allowlist": wanted if current is None else current & wanted}
        )


def _as_dict(g: Guardrails) -> dict[str, Any]:
    return {f: getattr(g, f) for f in Guardrails.__slots__}


def from_settings(settings: Any) -> Guardrails:
    return Guardrails(
        max_tool_calls=settings.max_tool_calls,
        max_workflow_steps=settings.max_workflow_steps,
        tool_timeout_seconds=settings.tool_timeout_seconds,
    )
