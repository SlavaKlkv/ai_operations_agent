"""Фабрика приложения FastAPI."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agent import checkpointing
from app.api.routes import github, health, integrations, metrics, runs, setup
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.migrations import migrate
from app.mcp import runtime as mcp_runtime
from app.observability import recording
from app.web.routes import STATIC_DIR
from app.web.routes import router as web_router

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("application.start", environment=settings.app_env)

    if settings.app_env != "test":
        await migrate(settings)
        log.info("database.migrated", backend=settings.storage_backend)
        # Каталог runbook находится на том же постоянном volume, что и SQLite.
        # Создаём его при каждом старте: volume мог появиться на предыдущей
        # версии образа, где этого каталога ещё не существовало.
        settings.runbooks_dir.mkdir(parents=True, exist_ok=True)

    saver = await checkpointing.startup(settings)
    log.info("checkpointer.attached", durable=checkpointing.is_durable(), kind=type(saver).__name__)

    if settings.mcp_enabled:
        # Подключаемся здесь, а не при каждом запросе: каждый stdio-сервер — отдельный
        # процесс, а деградация слоя интеграции отображается в /mcp/servers,
        # не мешая запуску приложения.
        pool = await mcp_runtime.startup()
        log.info("mcp.ready", healthy=pool.healthy, tools=len(pool.tools()))
        recording.record_integration_health(
            pool.status, durable_checkpointer=checkpointing.is_durable()
        )
    else:
        recording.record_integration_health((), durable_checkpointer=checkpointing.is_durable())
    try:
        yield
    finally:
        await setup.download_manager.cancel()
        await github.shutdown_connectors()
        await mcp_runtime.shutdown()
        await checkpointing.shutdown()
        log.info("application.stop")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Operations Agent",
        version="0.1.0",
        summary="Agentic incident analysis with human-approved write actions.",
        description=(
            "Investigates backend incidents by correlating deployments, metrics, logs and "
            "code changes, then proposes an action that a human must approve."
        ),
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(runs.router)
    app.include_router(integrations.router)
    app.include_router(metrics.router)
    app.include_router(setup.router)
    app.include_router(github.router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
