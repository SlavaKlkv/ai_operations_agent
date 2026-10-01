"""Small Ollama boundary used by setup diagnostics and model validation."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx


class OllamaUnavailable(RuntimeError):
    """Ollama could not be reached or returned an invalid response."""


class ModelCompatibilityError(RuntimeError):
    """An installed custom model failed a required capability check."""


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
        """Require both tool calling and schema-constrained JSON before saving."""
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
