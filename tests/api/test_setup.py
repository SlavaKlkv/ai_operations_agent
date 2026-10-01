"""First-run diagnostics and persisted model selection."""

from __future__ import annotations

from app.api.routes.setup import get_ollama_client
from app.services.ollama import OllamaSnapshot


class FakeOllama:
    def __init__(self, *models: str, available: bool = True) -> None:
        self.snapshot_value = OllamaSnapshot(available=available, models=tuple(models))
        self.verified: list[str] = []

    async def snapshot(self) -> OllamaSnapshot:
        return self.snapshot_value

    async def verify_custom_model(self, model_name: str) -> None:
        self.verified.append(model_name)


async def test_setup_reports_actionable_local_state(app, client):
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllama("qwen3:4b", "qwen3:8b")

    response = await client.get("/setup")

    assert response.status_code == 200
    body = response.json()
    assert body["ollama"] == {"ready": True, "status": "Ollama доступен", "action": None}
    assert body["storage"]["ready"] is True
    assert body["model"]["selection"] == {
        "profile": "standard",
        "model_name": "qwen3:8b",
        "verified": True,
    }
    assert body["model"]["installed"] is True
    assert body["github"]["ready"] is False
    assert body["ready"] is False


async def test_profile_selection_is_persisted_and_snapshotted_on_new_runs(app, client):
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllama("qwen3:4b", "qwen3:8b")

    selected = await client.put("/setup/model", json={"profile": "light"})
    assert selected.status_code == 200
    assert selected.json()["selection"]["model_name"] == "qwen3:4b"

    created = await client.post(
        "/runs",
        json={"task": "После релиза billing-service возвращает много ошибок 5xx"},
    )
    assert created.status_code == 201
    assert created.json()["model_name"] == "qwen3:4b"

    await client.put("/setup/model", json={"profile": "standard"})
    stored = await client.get(f"/runs/{created.json()['id']}")
    assert stored.json()["model_name"] == "qwen3:4b"


async def test_custom_model_requires_smoke_test_and_keeps_unverified_label(app, client):
    ollama = FakeOllama("qwen3:8b", "local-special:latest")
    app.dependency_overrides[get_ollama_client] = lambda: ollama

    response = await client.put(
        "/setup/model",
        json={"profile": "custom", "model_name": "local-special:latest"},
    )

    assert response.status_code == 200
    assert ollama.verified == ["local-special:latest"]
    assert response.json()["selection"] == {
        "profile": "custom",
        "model_name": "local-special:latest",
        "verified": False,
    }


async def test_model_must_be_installed_before_it_can_be_selected(app, client):
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllama("qwen3:8b")

    response = await client.put("/setup/model", json={"profile": "light"})

    assert response.status_code == 409
    assert "не установлена" in response.json()["detail"]


async def test_read_only_user_cannot_change_the_runtime_model(app, reader_client):
    app.dependency_overrides[get_ollama_client] = lambda: FakeOllama("qwen3:4b", "qwen3:8b")

    response = await reader_client.put("/setup/model", json={"profile": "light"})

    assert response.status_code == 403
