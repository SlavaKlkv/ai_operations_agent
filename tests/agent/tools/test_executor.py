"""Защитные ограничения и путь выполнения, проверяемые в точке их применения."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from app.agent.guardrails import BudgetExhausted, Guardrails, RepetitionLimitExceeded
from app.agent.tools.base import (
    AgentTool,
    ToolAccess,
    ToolNotAllowedError,
    ToolRegistry,
    call_signature,
)
from app.agent.tools.catalog import build_registry
from app.agent.tools.executor import ToolExecutor, ToolRequest

WINDOW_START = datetime(2026, 3, 17, 14, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 3, 17, 15, 0, tzinfo=UTC)
DEFAULTS = {"start": WINDOW_START, "end": WINDOW_END}


@pytest.fixture
def executor(monitoring, code, logs):
    return ToolExecutor(build_registry(monitoring, code, logs), Guardrails())


class _Args(BaseModel):
    pass


class _Result(BaseModel):
    ok: bool = True


def _slow_tool(delay: float, access: ToolAccess = ToolAccess.READ) -> AgentTool:
    async def handler(args: _Args) -> _Result:
        await asyncio.sleep(delay)
        return _Result()

    return AgentTool(
        name="slow",
        description="slow",
        args_schema=_Args,
        result_schema=_Result,
        access=access,
        handler=handler,
        render=lambda r: "done",
    )


# ── Политика ─────────────────────────────────────────────────────────────────


def test_write_tools_are_invisible_until_approved():
    registry = ToolRegistry([_slow_tool(0, ToolAccess.WRITE)])
    assert Guardrails().available(registry) == ()
    assert len(Guardrails().with_write_approved().available(registry)) == 1


async def test_write_tool_is_refused_not_raised(monitoring, code, logs):
    """Отказ должен возвращаться записанным, маршрутизируемым исходом: граф
    должен уметь сказать пользователю, от чего он отказался."""
    registry = ToolRegistry([_slow_tool(0, ToolAccess.WRITE)])
    executor = ToolExecutor(registry, Guardrails())
    invocation = await executor.execute(ToolRequest(tool="slow", arguments={}))
    assert not invocation.ok
    assert "WriteNotApproved" in (invocation.error or "")


def test_allowlist_narrowing_is_intersecting_never_widening():
    base = Guardrails(allowlist=frozenset({"a", "b"}))
    assert base.narrowed_to({"b", "c"}).allowlist == frozenset({"b"})
    assert Guardrails().narrowed_to({"a"}).allowlist == frozenset({"a"})


def test_denylist_beats_allowlist():
    tool = _slow_tool(0)
    policy = Guardrails(allowlist=frozenset({"slow"}), denylist=frozenset({"slow"}))
    with pytest.raises(ToolNotAllowedError, match="denied"):
        policy.check_tool(tool)


def test_budget_checks_cover_both_tool_calls_and_steps():
    policy = Guardrails(max_tool_calls=2, max_workflow_steps=3)
    policy.check_budget(tool_calls=1, steps=1)
    with pytest.raises(BudgetExhausted, match="tool-call budget"):
        policy.check_budget(tool_calls=2, steps=1)
    with pytest.raises(BudgetExhausted, match="step budget"):
        policy.check_budget(tool_calls=0, steps=3)


def test_write_approval_is_the_only_thing_with_write_changes():
    policy = Guardrails(max_tool_calls=7, allowlist=frozenset({"x"}))
    approved = policy.with_write_approved()
    assert approved.allow_write is True
    assert approved.max_tool_calls == 7
    assert approved.allowlist == frozenset({"x"})


def test_repetition_is_detected_by_arguments_not_just_name():
    policy = Guardrails(max_identical_calls=2)
    a = call_signature("t", {"service": "billing"})
    b = call_signature("t", {"service": "search"})
    policy.check_repetition(a, [a, b])
    with pytest.raises(RepetitionLimitExceeded):
        policy.check_repetition(a, [a, a, b])


def test_signature_ignores_omitted_arguments_and_key_order():
    assert call_signature("t", {"b": 1, "a": 2, "c": None}) == call_signature("t", {"a": 2, "b": 1})


# ── Выполнение ───────────────────────────────────────────────────────────────


async def test_successful_call_returns_typed_result_and_bounded_digest(executor):
    invocation = await executor.execute(
        ToolRequest(
            tool="get_service_metrics",
            arguments={"service": "billing-service", "metric": "error_rate"},
        ),
        defaults=DEFAULTS,
    )
    assert invocation.ok
    assert invocation.result.series.metric == "error_rate"
    assert "peak" in invocation.digest
    assert invocation.record.arguments["start"] == WINDOW_START.isoformat()


async def test_window_defaults_are_only_applied_to_tools_that_take_one(executor):
    """get_pull_request запрещает лишние поля; предложение ему окна не
    должно превращать корректный вызов в ошибку валидации."""
    invocation = await executor.execute(
        ToolRequest(
            tool="get_pull_request", arguments={"service": "billing-service", "number": 482}
        ),
        defaults=DEFAULTS,
    )
    assert invocation.ok
    assert invocation.result.pull_request.number == 482
    assert "start" not in invocation.record.arguments


async def test_explicit_arguments_win_over_defaults(executor):
    narrow = WINDOW_END - timedelta(minutes=5)
    invocation = await executor.execute(
        ToolRequest(
            tool="get_service_metrics",
            arguments={
                "service": "billing-service",
                "metric": "error_rate",
                "start": narrow.isoformat(),
            },
        ),
        defaults=DEFAULTS,
    )
    assert invocation.ok
    assert invocation.result.series.points[0].timestamp >= narrow


async def test_unknown_tool_is_recorded_not_raised(executor):
    invocation = await executor.execute(ToolRequest(tool="rm_rf", arguments={}))
    assert not invocation.ok
    assert "UnknownToolError" in invocation.error
    assert executor.history == (), "a refused call must not count against repetition"


async def test_invalid_arguments_never_reach_the_provider(executor):
    invocation = await executor.execute(
        ToolRequest(
            tool="get_service_metrics",
            arguments={"service": "billing-service", "metric": "cpu_temperature"},
        ),
        defaults=DEFAULTS,
    )
    assert not invocation.ok
    assert "InvalidToolArgumentsError" in invocation.error


async def test_provider_failure_is_captured_as_a_failed_record(executor):
    invocation = await executor.execute(
        ToolRequest(
            tool="get_service_metrics",
            arguments={"service": "unknown-service", "metric": "error_rate"},
        ),
        defaults=DEFAULTS,
    )
    assert not invocation.ok
    assert "UnknownMetricError" in invocation.error
    assert invocation.record.attempt == 2, "transient failures are retried once"


async def test_timeout_is_enforced_by_the_executor_not_the_tool():
    executor = ToolExecutor(
        ToolRegistry([_slow_tool(0.5)]),
        Guardrails(tool_timeout_seconds=0.01, tool_retries=0),
    )
    invocation = await executor.execute(ToolRequest(tool="slow", arguments={}))
    assert not invocation.ok
    assert "timeout" in invocation.error


async def test_repeating_an_identical_call_is_eventually_refused(executor):
    request = ToolRequest(tool="get_recent_alerts", arguments={"service": "billing-service"})
    for _ in range(2):
        assert (await executor.execute(request, defaults=DEFAULTS)).ok
    refused = await executor.execute(request, defaults=DEFAULTS)
    assert not refused.ok
    assert "RepetitionLimitExceeded" in refused.error
