"""Durability guarantees for local and server checkpointers."""

from app.agent import checkpointing
from app.core.config import Settings


async def test_sqlite_checkpointer_is_durable_and_creates_its_parent(tmp_path):
    database = tmp_path / "nested" / "agent.db"
    settings = Settings(
        _env_file=None,
        checkpointer="sqlite",
        sqlite_path=database,
    )  # type: ignore[call-arg]

    await checkpointing.shutdown()
    saver = await checkpointing.startup(settings)
    try:
        assert type(saver).__name__ == "AsyncSqliteSaver"
        assert checkpointing.is_durable() is True
        assert database.exists()
    finally:
        await checkpointing.shutdown()
