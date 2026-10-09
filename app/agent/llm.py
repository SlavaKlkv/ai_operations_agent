"""Интеграция с моделью — единственное место, где агент общается с LLM.

Здесь LangChain оправдывает себя. Не как фреймворк, внутри которого написано
приложение, а как три конкретные абстракции, которые стоит не переизобретать:
интерфейс чат-модели, bind_tools для независимого от провайдера вызова
инструментов и with_structured_output для ответов, проверенных по схеме.
Всё остальное в этой кодовой базе намеренно на чистом Python.

Важнее, какой провайдер стоит за интерфейсом, два свойства:

Модель опциональна. build_chat_model возвращает None, когда
использование модели отключено, и граф тогда идёт своим детерминированным
путём. Тесты, CI и офлайн-демо работают без сервера Ollama, и — что полезнее —
отказ провайдера деградирует агента, а не останавливает его.

Каждый вызов ограничен. Таймауты, лимиты вывода и учёт токенов живут
здесь, поэтому ни одному узлу не нужно их помнить.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)


class LLMError(RuntimeError):
    """Модель отказала так, что вызывающей стороне приходится обходить это."""


class StructuredOutputError(LLMError):
    """Модель не смогла выдать вывод, соответствующий запрошенной схеме."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Учёт токенов одного вызова, накапливаемый на запуск."""

    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            latency_ms=round(self.latency_ms + other.latency_ms, 3),
        )

    @classmethod
    def from_message(cls, message: BaseMessage, latency_ms: float) -> Usage:
        meta = getattr(message, "usage_metadata", None) or {}
        return cls(
            input_tokens=int(meta.get("input_tokens", 0)),
            output_tokens=int(meta.get("output_tokens", 0)),
            latency_ms=round(latency_ms, 3),
        )


def build_chat_model(settings: Settings | None = None) -> BaseChatModel | None:
    """Настроенная чат-модель или None, если агент должен работать офлайн.

    Возврат None вместо возбуждения исключения намеренный: «модель
    недоступна» — нормальный режим работы этой системы, а не ошибка. Граф
    проверяет это один раз и выбирает детерминированный маршрут.
    """
    settings = settings or get_settings()
    if not settings.llm_enabled:
        log.info("llm.disabled", reason="llm_enabled=false")
        return None
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.ollama_base_url,
        num_predict=settings.llm_max_tokens,
        client_kwargs={"timeout": settings.llm_timeout_seconds},
        stop=None,
    )


async def invoke(
    runnable: Runnable[Any, Any], messages: Sequence[BaseMessage], **kwargs: Any
) -> tuple[AIMessage, Usage]:
    """Один вызов модели с замером времени и подсчётом токенов.

    Ошибки провайдера перевозбуждаются как LLMError, чтобы
    вызывающие маршрутизировали по одному типу исключения, а не по тому, что
    случится бросить у SDK.
    """
    started = time.perf_counter()
    try:
        response = await runnable.ainvoke(list(messages), **kwargs)
    except Exception as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    elapsed = (time.perf_counter() - started) * 1000
    if not isinstance(response, AIMessage):
        raise LLMError(f"expected an AIMessage, got {type(response).__name__}")
    return response, Usage.from_message(response, elapsed)


async def structured[T: BaseModel](
    model: BaseChatModel,
    schema: type[T],
    messages: Sequence[BaseMessage],
) -> tuple[T, Usage]:
    """Запросить значение schema и получить его — или громко упасть.

    with_structured_output уже ограничивает модель схемой, но провайдер
    всё равно может вернуть то, что не проходит валидацию. Преобразование
    этого в StructuredOutputError удерживает сбой внутри обработки
    ошибок графа, а не даёт ему всплыть случайным ValidationError где-то выше
    по стеку.
    """
    started = time.perf_counter()
    try:
        result = await model.with_structured_output(schema).ainvoke(list(messages))
    except ValidationError as exc:
        raise StructuredOutputError(
            f"{schema.__name__}: {exc.error_count()} field error(s)"
        ) from exc
    except Exception as exc:
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    elapsed = (time.perf_counter() - started) * 1000

    if not isinstance(result, schema):
        try:
            result = schema.model_validate(result)
        except ValidationError as exc:
            raise StructuredOutputError(
                f"{schema.__name__}: {exc.error_count()} field error(s)"
            ) from exc
    # Вызовы со структурированным выводом не передают здесь метаданные потребления,
    # но задержку всё равно стоит записать для наблюдаемости запуска.
    return result, Usage(latency_ms=round(elapsed, 3))


class ScriptedChatModel(BaseChatModel):
    """Чат-модель, воспроизводящая подготовленные ответы по порядку.

    Она существует, чтобы логику решений агента можно было тестировать и
    оценивать детерминированно: заскриптованный вызов инструмента — это
    единственный способ убедиться, что граф маршрутизирует, валидирует и
    ограничивает выбор модели корректно, не платя провайдеру и не принимая
    недетерминизм.

    Это настоящая BaseChatModel, поэтому bind_tools и
    with_structured_output ведут себя точно так же, как в продакшене.
    """

    responses: list[AIMessage] = Field(default_factory=list)
    #: Все списки сообщений, с которыми вызывалась модель, для проверки промптов.
    calls: list[list[BaseMessage]] = Field(default_factory=list)
    bound_tools: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BaseChatModel:
        self.bound_tools = list(tools)
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self.responses:
            raise LLMError("scripted model ran out of responses")
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])
