"""Контракт инструмента, через который выражается каждая возможность агента.

Инструмент — это не просто вызываемый объект. Это имя, проверенная схема
аргументов, проверенная схема результата, класс доступа (чтение или запись) и
ограниченная текстовая отрисовка для модели. Связывание всего этого вместе и
делает возможными защитные ограничения: реестр может отказать в неизвестном
инструменте, исполнитель может отвергнуть придуманные LLM аргументы, а
инструмент записи не может пойти тем же путём, что инструмент чтения.

LLM показывают схемы, но никогда не дают исполнения. Она отвечает именем
инструмента и аргументами; реестр решает, позволено ли это, и запускает.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolAccess(StrEnum):
    """Инструменты чтения выполняются автоматически. Инструменты записи требуют
    одобренного решения."""

    READ = "read"
    WRITE = "write"


class ToolError(RuntimeError):
    """Базовый класс для проблем, о которых исполнитель должен сообщать, а не пробрасывать их."""


class UnknownToolError(ToolError):
    """Модель запросила инструмент, которого нет в реестре."""


class ToolNotAllowedError(ToolError):
    """Инструмент существует, но вне allowlist этого запуска или его класса доступа."""


class InvalidToolArgumentsError(ToolError):
    """Аргументы не прошли валидацию по схеме до того, как инструмент вообще был вызван."""


class InvalidToolResultError(ToolError):
    """Провайдер вернул то, что отвергает схема результата инструмента."""


#: Максимальный объём результата одного инструмента в промпте. Хранилища логов
#: и метрик могут вернуть неограниченный объём; модель видит только агрегат,
#: который тоже обрезается.
MAX_DIGEST_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class AgentTool[A: BaseModel, R: BaseModel]:
    """Одна возможность, полностью описанная.

    handler получает проверенные аргументы и возвращает проверенный
    результат. render превращает этот результат в ограниченный текст,
    который модели разрешено видеть, — намеренно отдельно от самого результата,
    потому что граф хранит полный типизированный объект, а промпт получает
    сводку.
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
        """Описание инструмента в форме, которую ожидает любой tool-calling API LLM."""
        schema = self.args_schema.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


def call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Стабильная идентичность вызова: тот же инструмент, те же значимые аргументы.

    Пропущенные аргументы и порядок ключей не должны её менять, иначе
    обнаружение повторов обходилось бы переупорядочиванием словаря моделью.
    """
    rendered = ",".join(
        f"{k}={arguments[k]!r}" for k in sorted(arguments) if arguments[k] is not None
    )
    return f"{name}({rendered})"


def _compact(exc: ValidationError) -> str:
    """Одна строка на проблему валидации — читаемо для модели, удобно для логов."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()[:5]
    )


class ToolRequest(BaseModel):
    """Что планировщик решил сделать, прежде чем кто-либо проверил, можно ли это.

    Модель Pydantic, а не простой датакласс, потому что отложенные запросы
    живут в состоянии графа, а состояние должно переживать сериализацию в
    снимок запуска, из которого строится журнал аудита.
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
    """Набор инструментов, которые может использовать запуск, и единственный путь к ним.

    Реестр неизменяем после сборки. Сужение происходит выводом нового реестра
    (allowlisted, read_only), а не мутацией общего состояния,
    поэтому один запуск не может расширить разрешения другого.
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
        """Вывести реестр, ограниченный names, игнорируя имена, которых у нас нет."""
        wanted = set(names)
        return ToolRegistry(t for t in self._tools.values() if t.name in wanted)

    def read_only(self) -> ToolRegistry:
        return ToolRegistry(t for t in self._tools.values() if not t.is_write)

    def by_access(self, access: ToolAccess) -> tuple[AgentTool[Any, Any], ...]:
        return tuple(t for t in self._tools.values() if t.access is access)

    def json_schemas(self) -> list[dict[str, Any]]:
        return [tool.json_schema() for tool in self._tools.values()]
