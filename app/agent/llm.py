"""Model integration — the one place the agent talks to an LLM.

This is where LangChain earns its place. Not as a framework the application is
written inside, but as three specific abstractions worth not re-implementing:
a chat-model interface, ``bind_tools`` for provider-neutral tool calling, and
``with_structured_output`` for schema-validated responses. Everything else in
this codebase is plain Python on purpose.

Two properties matter more than which provider is behind the interface:

*The model is optional.* :func:`build_chat_model` returns ``None`` when model
use is disabled, and the graph then runs its deterministic path. Tests, CI and
an offline demo all work without an Ollama server, and — more usefully — a
provider outage degrades the agent instead of stopping it.

*Every call is bounded.* Timeouts, output limits and token accounting live
here, so no node has to remember them.
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
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)


class LLMError(RuntimeError):
    """The model failed in a way the caller has to route around."""


class StructuredOutputError(LLMError):
    """The model could not produce output matching the requested schema."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one call, accumulated onto the run."""

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
    """The configured chat model, or ``None`` if the agent must run offline.

    Returning ``None`` rather than raising is deliberate: "no model available"
    is a normal operating mode for this system, not an error. The graph checks
    for it once and picks its deterministic route.
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
    model: BaseChatModel, messages: Sequence[BaseMessage], **kwargs: Any
) -> tuple[AIMessage, Usage]:
    """One model call, timed and token-counted.

    Provider errors are re-raised as :class:`LLMError` so that callers route on
    one exception type rather than on whatever the SDK happens to throw.
    """
    started = time.perf_counter()
    try:
        response = await model.ainvoke(list(messages), **kwargs)
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
    """Ask for a value of ``schema`` and get one, or fail loudly.

    ``with_structured_output`` already constrains the model to the schema, but
    a provider can still return something that fails validation. Converting
    that into :class:`StructuredOutputError` keeps the failure inside the
    graph's error handling instead of surfacing as a stray ValidationError
    somewhere up the stack.
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
    """A chat model that replays prepared responses, in order.

    It exists so the agent's decision logic can be tested and evaluated
    deterministically: a scripted tool call is the only way to assert that the
    graph routes, validates and bounds a model's choice correctly without
    paying a provider and accepting nondeterminism.

    It is a real :class:`BaseChatModel`, so ``bind_tools`` and
    ``with_structured_output`` behave exactly as they do in production.
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
