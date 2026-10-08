"""Единственная дверь между решением вызвать инструмент и его выполнением.

Ничто в графе не вызывает провайдера напрямую. Запрос инструмента — откуда бы
он ни пришёл, от LLM или от детерминированного кода — приходит сюда как имя и
словарь, а уходит проверенным, измеренным по времени, учтённым по бюджету и
записанным в аудит вызовом.

Порядок операций и есть проектное решение: сначала политика, затем валидация
аргументов, затем исполнение с таймаутом, затем валидация результата. Запрос,
проваливший раннюю проверку, никогда не доходит до провайдера, а сбой
возвращается как данные, по которым граф может маршрутизировать, а не
пробрасывается через рабочий процесс.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel

from app.agent.guardrails import Guardrails, GuardrailViolation
from app.agent.state import ToolCallRecord
from app.agent.tooling import call_tool
from app.agent.tools.base import (
    AgentTool,
    InvalidToolArgumentsError,
    ToolError,
    ToolRegistry,
    ToolRequest,
    UnknownToolError,
    call_signature,
)
from app.services.cache import NullCache, ToolCache, cache_key

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Исход в трёх формах, в которых он нужен системе.

    result — типизированный объект, по которому рассуждает граф;
    digest — ограниченный текст, который модели разрешено читать;
    record — строка аудита. Их раздельность и не даёт сырому выводу
    провайдера случайно просочиться в промпт.
    """

    request: ToolRequest
    record: ToolCallRecord
    result: BaseModel | None = None
    digest: str = ""

    @property
    def ok(self) -> bool:
        return self.record.ok

    @property
    def error(self) -> str | None:
        return self.record.error


class ToolExecutor:
    """Реестр + политика + тайминг, связанные вместе на время жизни запуска."""

    def __init__(
        self,
        registry: ToolRegistry,
        guardrails: Guardrails,
        *,
        history: Sequence[str] = (),
        cache: ToolCache | None = None,
        cache_ttl: int = 60,
    ) -> None:
        self._registry = registry
        self._guardrails = guardrails
        self._history: list[str] = list(history)
        self._cache = cache or NullCache()
        self._cache_ttl = cache_ttl

    @classmethod
    def resume(
        cls,
        registry: ToolRegistry,
        guardrails: Guardrails,
        records: Iterable[ToolCallRecord],
        *,
        cache: ToolCache | None = None,
        cache_ttl: int = 60,
    ) -> ToolExecutor:
        """Пересобрать исполнитель посреди запуска из того, что уже записано в состоянии.

        Узлы графа разделяются между параллельными запусками, поэтому
        исполнитель не может быть долгоживущим объектом, хранящим историю
        одного запуска. Пересборка его из состояния каждый раз сохраняет работу
        обнаружения повторов на протяжении цикла, пока сам граф остаётся без
        состояния и повторно входимым.
        """
        return cls(
            registry,
            guardrails,
            history=[call_signature(r.tool, r.arguments) for r in records],
            cache=cache,
            cache_ttl=cache_ttl,
        )

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def guardrails(self) -> Guardrails:
        return self._guardrails

    @property
    def history(self) -> tuple[str, ...]:
        """Сигнатуры всего, что было предпринято, по порядку."""
        return tuple(self._history)

    def available(self) -> tuple[AgentTool[Any, Any], ...]:
        return self._guardrails.available(self._registry)

    def authorise(self, request: ToolRequest) -> AgentTool[Any, Any]:
        """Выполнить все проверки политики. Возбуждает исключение, а не возвращает
        вердикт, чтобы вызывающий не мог случайно проигнорировать отказ."""
        tool = self._registry.get(request.tool)
        self._guardrails.check_tool(tool)
        self._guardrails.check_repetition(request.signature, self._history)
        return tool

    async def execute(
        self,
        request: ToolRequest,
        *,
        defaults: dict[str, Any] | None = None,
    ) -> ToolInvocation:
        """Авторизовать, проверить, выполнить и записать один вызов инструмента.

        defaults заполняют аргументы, которые планировщик опустил, — на
        практике временное окно расследования. Они применяются до валидации и
        никогда не переопределяют то, что планировщик задал явно.
        """
        try:
            tool = self._registry.get(request.tool)
        except UnknownToolError as exc:
            return self._refused(request, exc)

        arguments = _merge_defaults(request.arguments, defaults, tool.args_schema)
        request = request.model_copy(update={"arguments": arguments})

        try:
            self.authorise(request)
        except (UnknownToolError, GuardrailViolation, ToolError) as exc:
            return self._refused(request, exc)

        self._history.append(request.signature)

        try:
            parsed = tool.parse_arguments(arguments)
        except InvalidToolArgumentsError as exc:
            return self._refused(request, exc)

        # Только чтение. Запись имеет эффект, который нельзя получить из кэша;
        # решение принимает класс доступа, а не имя инструмента.
        key = cache_key(tool.name, arguments) if not tool.is_write else None
        if key is not None and (cached := await self._cache.get(key)) is not None:
            try:
                validated = tool.parse_result(cached)
            except ToolError:
                # Если значение из кэша больше не соответствует схеме, формат
                # инструмента изменился; продолжаем и получаем данные заново.
                log.info("cache.stale_shape", tool=tool.name)
            else:
                digest = tool.digest(validated)
                return ToolInvocation(
                    request=request,
                    record=ToolCallRecord(
                        tool=tool.name,
                        arguments=arguments,
                        started_at=datetime.now(UTC),
                        duration_ms=0.0,
                        ok=True,
                        result_summary=digest[:500],
                        cached=True,
                    ),
                    result=validated,
                    digest=digest,
                )

        outcome = await call_tool(
            tool.name,
            lambda: tool.handler(parsed),
            arguments=arguments,
            timeout=self._guardrails.tool_timeout_seconds,
            retries=self._guardrails.tool_retries,
        )
        if outcome.value is None:
            return ToolInvocation(request=request, record=outcome.record)

        try:
            validated = tool.parse_result(outcome.value)
        except ToolError as exc:
            return self._refused(request, exc, duration_ms=outcome.record.duration_ms)

        if key is not None:
            await self._cache.set(key, validated.model_dump(mode="json"), ttl=self._cache_ttl)

        digest = tool.digest(validated)
        record = outcome.record.model_copy(update={"result_summary": digest[:500]})
        return ToolInvocation(request=request, record=record, result=validated, digest=digest)

    def _refused(
        self, request: ToolRequest, exc: Exception, *, duration_ms: float = 0.0
    ) -> ToolInvocation:
        """Отказ — это всё ещё записанный вызов инструмента: журнал аудита должен
        показывать, что агент пытался сделать, а не только что ему было позволено."""
        return ToolInvocation(
            request=request,
            record=ToolCallRecord(
                tool=request.tool,
                arguments=_jsonable(request.arguments),
                started_at=datetime.now(UTC),
                duration_ms=duration_ms,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            ),
        )


def _merge_defaults(
    arguments: dict[str, Any],
    defaults: dict[str, Any] | None,
    args_schema: type[BaseModel],
) -> dict[str, Any]:
    """Заполнить опущенные аргументы, но лишь те, что инструмент объявляет.

    Фильтрация по схеме важна: значения окна по умолчанию предлагаются каждому
    вызову, а инструменту, не принимающему окно (get_pull_request), его
    передавать нельзя — его схема запрещает лишние поля и отвергла бы вызов.
    """
    merged = dict(arguments)
    for key, value in (defaults or {}).items():
        if key in args_schema.model_fields and merged.get(key) is None:
            merged[key] = value
    return _jsonable(merged)


def _jsonable(arguments: dict[str, Any]) -> dict[str, Any]:
    """Аргументы нормализуются к примитивам JSON как можно раньше.

    Они хранятся в JSON-колонках, образуют сигнатуру вызова, используемую для
    обнаружения повторов, и должны пережить круговой рейс через состояние —
    всё три ломается, если просочится объект datetime. Pydantic разбирает
    ISO-строки обратно при валидации, так что ничего не теряется.
    """
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in arguments.items()
    }
