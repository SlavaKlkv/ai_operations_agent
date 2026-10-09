"""Небольшая граница Ollama, используемая диагностикой настройки и проверкой моделей."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


class OllamaUnavailable(RuntimeError):
    """Ollama недоступна или вернула некорректный ответ."""


class ModelCompatibilityError(RuntimeError):
    """Установленная пользовательская модель не прошла обязательную проверку возможностей."""


@dataclass(frozen=True, slots=True)
class OllamaSnapshot:
    available: bool
    models: tuple[str, ...] = ()
    error: str | None = None


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def snapshot(self) -> OllamaSnapshot:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
            models = tuple(
                sorted(
                    str(item.get("name")) for item in payload.get("models", []) if item.get("name")
                )
            )
            return OllamaSnapshot(available=True, models=models)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return OllamaSnapshot(available=False, error=f"{type(exc).__name__}: {exc}")

    async def verify_custom_model(self, model_name: str) -> None:
        """Требовать и вызов инструментов, и JSON с ограничением схемой перед сохранением."""
        tool_payload = {
            "model": model_name,
            "stream": False,
            "messages": [{"role": "user", "content": "Use report_status to report ready."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "report_status",
                        "description": "Report the setup status.",
                        "parameters": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                            "required": ["status"],
                        },
                    },
                }
            ],
            "options": {"temperature": 0},
        }
        schema = {
            "type": "object",
            "properties": {"ready": {"type": "boolean"}},
            "required": ["ready"],
        }
        structured_payload = {
            "model": model_name,
            "stream": False,
            "messages": [{"role": "user", "content": "Return ready=true."}],
            "format": schema,
            "options": {"temperature": 0},
        }
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 60.0), transport=self._transport
            ) as client:
                tool_response = await client.post(f"{self._base_url}/api/chat", json=tool_payload)
                tool_response.raise_for_status()
                calls = tool_response.json().get("message", {}).get("tool_calls", [])
                if not any(
                    call.get("function", {}).get("name") == "report_status" for call in calls
                ):
                    raise ModelCompatibilityError("модель не выполнила tool calling")

                structured_response = await client.post(
                    f"{self._base_url}/api/chat", json=structured_payload
                )
                structured_response.raise_for_status()
                content = structured_response.json().get("message", {}).get("content", "")
                value = json.loads(content)
                if value.get("ready") is not True:
                    raise ModelCompatibilityError("структурированный ответ не прошёл проверку")
        except ModelCompatibilityError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ModelCompatibilityError(f"{type(exc).__name__}: {exc}") from exc

    async def pull_model(self, model_name: str) -> AsyncIterator[dict[str, Any]]:
        """Потоково читать разделённый по строкам прогресс загрузки Ollama,
        не буферизуя модель."""
        timeout = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout, transport=self._transport) as client,
                client.stream(
                    "POST", f"{self._base_url}/api/pull", json={"model": model_name}
                ) as response,
            ):
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("invalid Ollama pull progress")
                    yield payload
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise OllamaUnavailable("Загрузка модели Ollama прервана. Повторите попытку.") from exc
