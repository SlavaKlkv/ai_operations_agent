"""Durability guarantees for local and server checkpointers."""

from langgraph.types import Command

from app.agent import checkpointing
from app.agent.graph import build_graph, run_config
from app.agent.state import ApprovalState
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


async def test_approval_resumes_after_the_sqlite_checkpointer_reopens(
    tmp_path, monitoring, code, logs, fresh_state
):
    database = tmp_path / "agent.db"
    settings = Settings(
        _env_file=None,
        checkpointer="sqlite",
        sqlite_path=database,
    )  # type: ignore[call-arg]
    config = run_config(fresh_state["run_id"])

    await checkpointing.shutdown()
    first_saver = await checkpointing.startup(settings)
    first_graph = build_graph(
        monitoring=monitoring,
        code=code,
        logs=logs,
        checkpointer=first_saver,
        use_llm=False,
    )
    paused = await first_graph.ainvoke(fresh_state, config)
    assert paused["__interrupt__"]
    await checkpointing.shutdown()

    second_saver = await checkpointing.startup(settings)
    second_graph = build_graph(
        monitoring=monitoring,
        code=code,
        logs=logs,
        checkpointer=second_saver,
        use_llm=False,
    )
    try:
        resumed = await second_graph.ainvoke(
            Command(resume={"approved": False, "decided_by": "sre@example.com"}),
            config,
        )
        assert resumed["approval_state"] is ApprovalState.REJECTED
        assert resumed["task"] == fresh_state["task"]
        assert resumed["tool_call_count"] > 1
    finally:
        await checkpointing.shutdown()
