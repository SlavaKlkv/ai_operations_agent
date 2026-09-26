"""The limits an agent run cannot talk its way out of.

Every restriction here is enforced in Python, before a tool runs, on arguments
the model supplied. None of it is expressed as an instruction in a prompt: a
prompt is a request, and the whole point of a guardrail is that it is not one.

The checks are ordered cheapest-first and each raises a distinct exception, so
the graph can react differently to "you may not do that" (terminal) and "you
have done that enough times" (stop looping, go and conclude).
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
    """A run attempted something its policy forbids."""


class BudgetExhausted(GuardrailViolation):
    """The run has spent its allowance of tool calls or workflow steps."""


class WriteNotApproved(GuardrailViolation):
    """A write tool was reached without an approved human decision."""


class RepetitionLimitExceeded(GuardrailViolation):
    """The same tool was called with the same arguments too many times.

    Loops that re-fetch identical data are the characteristic failure of a
    tool-calling agent that has run out of ideas. Detecting it as a policy
    violation converts an expensive infinite loop into a cheap terminal state.
    """


@dataclass(frozen=True, slots=True)
class Guardrails:
    """Per-run policy. Built from settings, never from model output."""

    max_tool_calls: int = 12
    max_workflow_steps: int = 30
    tool_timeout_seconds: float = 15.0
    #: Дополнительные попытки после первого неудачного вызова инструмента.
    tool_retries: int = 1
    #: ``None`` означает все инструменты реестра; множество сужает список.
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
        """The tools the model is allowed to see. It is never shown more."""
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
        """Policy for the single step that executes an approved action."""
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
