"""Обёртка инструмента — единственное место, где обеспечиваются таймауты и
повторы, поэтому её тестируют напрямую, а не через узел."""

from __future__ import annotations

import asyncio

from app.agent.tooling import call_tool


async def test_successful_call_is_recorded():
    outcome = await call_tool("noop", lambda: _value(42), summarise=lambda v: f"v={v}")
    assert outcome.ok and outcome.value == 42
    assert outcome.record.attempt == 1
    assert outcome.record.result_summary == "v=42"
    assert outcome.record.duration_ms >= 0


async def test_timeout_is_bounded_and_reported():
    outcome = await call_tool("slow", _forever, timeout=0.01, retries=0)
    assert not outcome.ok and outcome.value is None
    assert "timeout" in outcome.record.error


async def test_transient_failure_is_retried():
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ConnectionError("mcp server unavailable")
        return "ok"

    outcome = await call_tool("flaky", flaky, retries=2)
    assert outcome.ok and outcome.value == "ok"
    assert outcome.record.attempt == 2


async def test_permanent_failure_stops_after_the_budget():
    attempts = {"n": 0}

    async def broken() -> str:
        attempts["n"] += 1
        raise ValueError("malformed response")

    outcome = await call_tool("broken", broken, retries=2)
    assert not outcome.ok
    assert attempts["n"] == 3  # первая попытка и два повтора
    assert outcome.record.error.startswith("ValueError")


async def test_arguments_are_kept_for_the_audit_trail():
    outcome = await call_tool("t", lambda: _value(1), arguments={"service": "billing-service"})
    assert outcome.record.arguments == {"service": "billing-service"}


async def _value(v):
    return v


async def _forever():
    await asyncio.sleep(10)
