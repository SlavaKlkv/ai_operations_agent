"""Application configuration loaded from the environment."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"

    storage_backend: Literal["sqlite", "postgres"] = "sqlite"
    sqlite_path: Path = Path("data/ai_operations_agent.db")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "agent"
    postgres_password: str = "agent"
    postgres_db: str = "ai_operations_agent"

    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")  # type: ignore[assignment]
    cache_backend: Literal["memory", "redis"] = "memory"

    # ── LLM ──────────────────────────────────────────────────────────────────
    #: При отключении агент выполняет детерминированный сценарий.
    llm_enabled: bool = True
    llm_model: str = "qwen3:8b"
    ollama_base_url: str = "http://localhost:11434"
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 60.0

    #: Требовать API-токен. Отключается только для локальной разработки; /health
    #: показывает настройку, чтобы случайное отключение было заметно.
    auth_enabled: bool = True

    #: Кэшировать результаты инструментов чтения между запусками. При отключении
    #: каждое расследование полностью обращается ко всем внешним системам.
    cache_enabled: bool = True
    #: Интервал намеренно короткий: окно, включающее текущий момент, ещё меняется.
    cache_ttl_seconds: int = 60

    #: SQLite — локальный долговечный режим, PostgreSQL — серверный, memory — только тесты.
    checkpointer: Literal["sqlite", "postgres", "memory"] = "sqlite"

    # ── Слой интеграции MCP ──────────────────────────────────────────────────
    #: Отключается в тестах и минимальном развёртывании: тогда агент работает с
    #: встроенными тестовыми провайдерами вместо четырёх MCP-серверов.
    mcp_enabled: bool = True

    # ── Ограничения агента ───────────────────────────────────────────────────
    max_tool_calls: int = 12
    max_workflow_steps: int = 30
    tool_timeout_seconds: float = 15.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def postgres_dsn(self) -> PostgresDsn:
        return PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            path=self.postgres_db,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_dsn(self) -> str:
        if self.storage_backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        return str(self.postgres_dsn)


@lru_cache
def get_settings() -> Settings:
    return Settings()
