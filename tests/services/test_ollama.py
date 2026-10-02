"""The exact local Ollama HTTP contract used by setup."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.ollama import ModelCompatibilityError, OllamaClient


async def test_snapshot_lists_installed_model_names():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200,
            json={"models": [{"name": "qwen3:8b"}, {"name": "qwen3:4b"}]},
        )

    snapshot = await OllamaClient(
        "http://ollama.test:11434",
        transport=httpx.MockTransport(handler),
    ).snapshot()

    assert snapshot.available is True
    assert snapshot.models == ("qwen3:4b", "qwen3:8b")


async def test_snapshot_turns_provider_failure_into_actionable_state():
    transport = httpx.MockTransport(lambda _request: httpx.Response(503))

    snapshot = await OllamaClient("http://ollama.test", transport=transport).snapshot()

    assert snapshot.available is False
    assert snapshot.models == ()
    assert "503" in snapshot.error


async def test_custom_model_verification_requires_both_capabilities():
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "tools" in payload:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "report_status",
                                    "arguments": {"status": "ready"},
                                }
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"message": {"content": '{"ready": true}'}})

    client = OllamaClient(
        "http://ollama.test",
        transport=httpx.MockTransport(handler),
    )
    await client.verify_custom_model("local-special:latest")

    assert [payload["model"] for payload in requests] == [
        "local-special:latest",
        "local-special:latest",
    ]
    assert requests[0]["tools"][0]["function"]["name"] == "report_status"
    assert requests[1]["format"]["required"] == ["ready"]


async def test_custom_model_without_tool_calling_is_rejected():
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"message": {"content": "ready"}})
    )

    with pytest.raises(ModelCompatibilityError, match="tool calling"):
        await OllamaClient("http://ollama.test", transport=transport).verify_custom_model(
            "text-only:latest"
        )


async def test_pull_streams_progress_and_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/pull"
        assert json.loads(request.content) == {"model": "qwen3:4b"}
        return httpx.Response(
            200,
            text=(
                '{"status":"pulling manifest"}\n'
                '{"status":"pulling layer","completed":50,"total":100}\n'
                '{"status":"success"}\n'
            ),
        )

    client = OllamaClient("http://ollama.test", transport=httpx.MockTransport(handler))
    updates = [item async for item in client.pull_model("qwen3:4b")]
    assert updates[-1]["status"] == "success"
    assert updates[1]["completed"] == 50
