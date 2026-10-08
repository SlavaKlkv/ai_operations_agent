"""Граница модели: необязательность, перевод ошибок, учёт использования."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from app.agent.llm import (
    LLMError,
    ScriptedChatModel,
    StructuredOutputError,
    Usage,
    build_chat_model,
    invoke,
    structured,
)
from app.core.config import Settings


class Answer(BaseModel):
    service: str
    confident: bool


def test_llm_can_be_switched_off_without_contacting_ollama():
    settings = Settings(llm_enabled=False)
    assert build_chat_model(settings) is None


def test_standard_profile_model_is_the_default():
    assert Settings(_env_file=None).llm_model == "qwen3:8b"  # type: ignore[call-arg]


def test_local_storage_is_the_default(tmp_path):
    settings = Settings(
        _env_file=None,
        storage_backend="sqlite",
        sqlite_path=tmp_path / "agent.db",
    )  # type: ignore[call-arg]

    assert Settings.model_fields["storage_backend"].default == "sqlite"
    assert settings.database_dsn == f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}"
    assert Settings.model_fields["cache_backend"].default == "memory"
    assert Settings.model_fields["checkpointer"].default == "sqlite"


def test_configured_model_is_built_lazily():
    model = build_chat_model(
        Settings(
            llm_enabled=True,
            llm_model="qwen3:4b",
            ollama_base_url="http://ollama.test:11434",
        )
    )
    assert model is not None
    assert model.model == "qwen3:4b"
    assert model.base_url == "http://ollama.test:11434"


async def test_provider_errors_are_translated_to_one_exception_type():
    """Вызывающий код реагирует на LLMError; он не должен знать классы SDK."""
    model = ScriptedChatModel(responses=[])
    with pytest.raises(LLMError):
        await invoke(model, [HumanMessage("hello")])


async def test_structured_output_returns_a_validated_model():
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "Answer", "args": {"service": "billing", "confident": True}, "id": "1"}
                ],
            )
        ]
    )
    answer, usage = await structured(model, Answer, [HumanMessage("who")])
    assert answer == Answer(service="billing", confident=True)
    assert usage.latency_ms > 0


async def test_output_that_does_not_match_the_schema_is_a_structured_error():
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "Answer", "args": {"service": "billing"}, "id": "1"}],
            )
        ]
    )
    with pytest.raises(StructuredOutputError):
        await structured(model, Answer, [HumanMessage("who")])


async def test_usage_is_read_from_the_provider_and_accumulates():
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="done",
                usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
            )
        ]
    )
    _, usage = await invoke(model, [HumanMessage("hi")])
    assert (usage.input_tokens, usage.output_tokens) == (120, 30)
    assert (usage + Usage(input_tokens=5)).total_tokens == 155


async def test_the_scripted_model_records_what_it_was_asked():
    """Содержимое промпта проверяется в других тестах; здесь — механизм."""
    model = ScriptedChatModel(responses=[AIMessage(content="ok")])
    await invoke(model, [HumanMessage("the briefing")])
    assert model.calls[0][0].content == "the briefing"
