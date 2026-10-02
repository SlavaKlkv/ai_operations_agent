"""Local model-pull lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from app.services.model_download import ModelDownloadManager


class FakeOllama:
    async def pull_model(self, model):
        yield {"status": "pulling", "total": 100, "completed": 40}
        yield {"status": "success"}


async def test_download_reaches_complete_with_progress():
    manager = ModelDownloadManager()
    state = manager.start("qwen3:4b", FakeOllama())
    assert state.model == "qwen3:4b"
    await manager.task
    assert state.state == "complete"
    assert state.view()["completed"] == 100


async def test_second_download_is_rejected_while_first_runs():
    started = asyncio.Event()

    class SlowOllama:
        async def pull_model(self, model):
            started.set()
            await asyncio.Event().wait()
            yield {"status": "success"}

    manager = ModelDownloadManager()
    manager.start("qwen3:4b", SlowOllama())
    await started.wait()
    with pytest.raises(ValueError, match="уже загружается"):
        manager.start("qwen3:8b", SlowOllama())
    await manager.cancel()
    assert manager.current.state == "cancelled"


async def test_missing_success_is_failure():
    class IncompleteOllama:
        async def pull_model(self, model):
            yield {"status": "pulling"}

    manager = ModelDownloadManager()
    state = manager.start("qwen3:4b", IncompleteOllama())
    await manager.task
    assert state.state == "failed"
