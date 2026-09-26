"""The tool contract every capability of the agent is expressed through.

A tool is not just a callable. It is a name, a validated argument schema, a
validated result schema, an access class (read or write) and a bounded textual
rendering for the model. Bundling those together is what makes the guardrails
possible: the registry can refuse an unknown tool, the executor can reject
arguments the LLM invented, and a write tool cannot run down the same path a
read tool does.

The LLM is shown *schemas*, never given execution. It answers with a tool name
and arguments; the registry decides whether that is allowed and runs it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolAccess(StrEnum):
    """Read tools run automatically. Write tools require an approved decision."""

    READ = "read"
    WRITE = "write"


class ToolError(RuntimeError):
    """Base class for problems the executor must report rather than raise through."""


class UnknownToolError(ToolError):
    """The model asked for a tool that is not in the registry."""


class ToolNotAllowedError(ToolError):
    """The tool exists but is outside this run's allowlist or access class."""


class InvalidToolArgumentsError(ToolError):
    """Arguments failed schema validation before the tool was ever called."""


class InvalidToolResultError(ToolError):
    """A provider returned something the tool's result schema rejects."""


#: Максимальный объём результата одного инструмента в промпте. Хранилища логов
#: и метрик могут вернуть неограниченный объём; модель видит только агрегат,
#: который тоже обрезается.
MAX_DIGEST_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class AgentTool[A: BaseModel, R: BaseModel]:
    """One capability, fully described.

    ``handler`` receives validated arguments and returns a validated result.
    ``render`` turns that result into the bounded text the model is allowed to
    see — deliberately separate from the result itself, because the graph keeps
    the full typed object while the prompt gets a digest.
    """

    name: str
    description: str
    args_schema: type[A]
    result_schema: type[R]
    access: ToolAccess
    handler: Callable[[A], Awaitable[R]]
    render: Callable[[R], str]
    #: Оценка стоимости, помогающая планировщику предпочитать дешёвые доказательства.
    cost: int = 1

    @property
    def is_write(self) -> bool:
        return self.access is ToolAccess.WRITE

    def parse_arguments(self, raw: dict[str, Any]) -> A:
        try:
            return self.args_schema.model_validate(raw)
        except ValidationError as exc:
            raise InvalidToolArgumentsError(f"{self.name}: {_compact(exc)}") from exc

    def parse_result(self, value: Any) -> R:
        try:
            return self.result_schema.model_validate(value)
        except ValidationError as exc:
            raise InvalidToolResultError(f"{self.name}: {_compact(exc)}") from exc

    def digest(self, result: R) -> str:
        text = self.render(result)
        if len(text) <= MAX_DIGEST_CHARS:
            return text
        return text[: MAX_DIGEST_CHARS - 1].rstrip() + "…"

    def json_schema(self) -> dict[str, Any]:
        """Tool description in the shape every tool-calling LLM API expects."""
        schema = self.args_schema.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


def call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Stable identity of a call: same tool, same meaningful arguments.

    Omitted arguments and key order must not change it, otherwise repetition
    detection would be defeated by the model reordering a dict.
    """
    rendered = ",".join(
        f"{k}={arguments[k]!r}" for k in sorted(arguments) if arguments[k] is not None
    )
    return f"{name}({rendered})"


def _compact(exc: ValidationError) -> str:
    """One line per validation problem — model-readable, log-friendly."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()[:5]
    )


class ToolRequest(BaseModel):
    """What the planner decided to do, before anyone has checked whether it may.

    A Pydantic model rather than a plain dataclass because pending requests
    live in the graph state, and state has to survive serialisation into the
    run snapshot that the audit trail is built from.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Зачем это нужно планировщику. Сохраняется для аудита и оценки.
    reason: str = ""

    @property
    def signature(self) -> str:
        return call_signature(self.tool, self.arguments)


class ToolRegistry:
    """The set of tools a run may use, and the only way to reach them.

    A registry is immutable once built. Narrowing happens by deriving a new
    registry (:meth:`allowlisted`, :meth:`read_only`) rather than by mutating
    shared state, so one run cannot widen another run's permissions.
    """

    def __init__(self, tools: Iterable[AgentTool[Any, Any]]) -> None:
        self._tools: dict[str, AgentTool[Any, Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[AgentTool[Any, Any]]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def get(self, name: str) -> AgentTool[Any, Any]:
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "<none>"
            raise UnknownToolError(f"unknown tool {name!r}; available: {known}") from None

    def allowlisted(self, names: Iterable[str]) -> ToolRegistry:
        """Derive a registry limited to ``names``, ignoring names we do not have."""
        wanted = set(names)
        return ToolRegistry(t for t in self._tools.values() if t.name in wanted)

    def read_only(self) -> ToolRegistry:
        return ToolRegistry(t for t in self._tools.values() if not t.is_write)

    def by_access(self, access: ToolAccess) -> tuple[AgentTool[Any, Any], ...]:
        return tuple(t for t in self._tools.values() if t.access is access)

    def json_schemas(self) -> list[dict[str, Any]]:
        return [tool.json_schema() for tool in self._tools.values()]
