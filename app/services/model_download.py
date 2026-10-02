"""One bounded, cancellable model download for the local setup wizard."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from app.services.ollama import OllamaClient, OllamaUnavailable


@dataclass
class DownloadState:
    model: str
    state: str = "running"
    status: str = "Подключаемся к Ollama…"
    completed: int = 0
    total: int = 0
    error: str | None = None

    def view(self) -> dict[str, str | int | None]:
        return {
            "model": self.model,
            "state": self.state,
            "status": self.status,
            "completed": self.completed,
            "total": self.total,
            "error": self.error,
        }


class ModelDownloadManager:
    def __init__(self) -> None:
        self.current: DownloadState | None = None
        self.task: asyncio.Task[None] | None = None

    def start(self, model: str, client: OllamaClient) -> DownloadState:
        if self.task is not None and not self.task.done():
            raise ValueError("Другая модель уже загружается.")
        self.current = DownloadState(model=model)
        self.task = asyncio.create_task(self._run(client, self.current))
        return self.current

    async def _run(self, client: OllamaClient, state: DownloadState) -> None:
        try:
            async for item in client.pull_model(state.model):
                state.status = str(item.get("status", "Загружаем…"))[:200]
                state.completed = max(0, int(item.get("completed", state.completed)))
                state.total = max(0, int(item.get("total", state.total)))
                if item.get("error"):
                    raise OllamaUnavailable("Ollama сообщила об ошибке загрузки модели.")
                if state.status == "success":
                    state.state = "complete"
                    if state.total:
                        state.completed = state.total
            if state.state != "complete":
                raise OllamaUnavailable("Ollama не подтвердила завершение загрузки.")
        except asyncio.CancelledError:
            state.state = "cancelled"
            state.status = "Загрузка отменена"
            raise
        except (OllamaUnavailable, ValueError, TypeError) as exc:
            state.state = "failed"
            state.error = str(exc)

    async def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
