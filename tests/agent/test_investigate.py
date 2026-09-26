"""The agentic cycle: selection, execution, and how the loop is made to end."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.guardrails import Guardrails
from app.agent.nodes.investigate import (
    MAX_LOOP_ITERATIONS,
    evaluate_observation_node,
    make_execute_tool_node,
    make_route_after_evaluation,
    make_select_tool_node,
    route_after_selection,
)
from app.agent.planner import HeuristicPlanner, Plan
from app.agent.state import CollectedContext, ToolCallRecord
from app.agent.tools.base import ToolRequest
from app.agent.tools.catalog import build_registry
from app.domain.models import EvidenceKind

WINDOW = (
    datetime(2026, 3, 17, 14, 0, tzinfo=UTC),
    datetime(2026, 3, 17, 15, 0, tzinfo=UTC),
)


@pytest.fixture
def registry(monitoring, code, logs):
    return build_registry(monitoring, code, logs)


def _state(**overrides):
    base = {
        "run_id": "r1",
        "task": "billing-service 5xx",
        "target_service": "billing-service",
        "window_start": WINDOW[0],
        "window_end": WINDOW[1],
        "context": CollectedContext(),
        "evidence": [],
        "hypotheses": [],
        "tool_calls": [],
        "observations": [],
        "step_count": 3,
        "tool_call_count": 6,
    }
    return base | overrides


class _FixedPlanner:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.seen: list[dict] = []

    async def plan(self, state, available):
        self.seen.append(dict(state))
        return self._plan


# ── select_tool ──────────────────────────────────────────────────────────────


async def test_selection_tells_the_planner_what_budget_is_left(registry):
    planner = _FixedPlanner(Plan())
    node = make_select_tool_node(planner, registry, Guardrails(max_tool_calls=10))
    await node(_state(tool_call_count=6))
    assert planner.seen[0]["tool_budget_remaining"] == 4


async def test_selection_refuses_to_plan_once_the_budget_is_spent(registry):
    """The check happens before the planner runs, so an exhausted run costs
    nothing further — not even a model call."""
    planner = _FixedPlanner(Plan(requests=(ToolRequest(tool="get_commits"),)))
    node = make_select_tool_node(planner, registry, Guardrails(max_tool_calls=6))
    result = await node(_state(tool_call_count=6))

    assert planner.seen == []
    assert result["pending_requests"] == []
    assert "budget spent" in result["planner_rationale"]


async def test_selection_never_queues_more_than_the_budget_allows(registry):
    plan = Plan(
        requests=(
            ToolRequest(tool="get_recent_alerts", arguments={"service": "billing-service"}),
            ToolRequest(tool="get_commits", arguments={"service": "billing-service"}),
        )
    )
    node = make_select_tool_node(_FixedPlanner(plan), registry, Guardrails(max_tool_calls=7))
    result = await node(_state(tool_call_count=6))
    assert len(result["pending_requests"]) == 1


async def test_a_planner_failure_is_recorded_as_a_recoverable_error(registry):
    node = make_select_tool_node(
        _FixedPlanner(Plan(error="LLMError: upstream 503")), registry, Guardrails()
    )
    result = await node(_state())
    assert result["errors"][0].kind == "planner_failed"
    assert result["errors"][0].recoverable is True
    assert result["pending_requests"] == []


def test_an_empty_plan_routes_straight_to_the_analysis():
    assert route_after_selection({"pending_requests": []}) == "generate_analysis"
    assert (
        route_after_selection({"pending_requests": [ToolRequest(tool="get_commits")]})
        == "execute_tool"
    )


# ── execute_tool ─────────────────────────────────────────────────────────────


async def test_execution_absorbs_results_into_typed_context(registry):
    node = make_execute_tool_node(registry, Guardrails())
    result = await node(
        _state(
            pending_requests=[
                ToolRequest(tool="get_recent_alerts", arguments={"service": "billing-service"})
            ]
        )
    )
    assert result["context"].alerts
    assert result["evidence"][0].kind is EvidenceKind.ALERT
    assert result["tool_call_count"] == 7
    assert result["pending_requests"] == [], "the queue is cleared after execution"


async def test_execution_injects_the_window_the_planner_omitted(registry):
    node = make_execute_tool_node(registry, Guardrails())
    result = await node(
        _state(
            pending_requests=[
                ToolRequest(
                    tool="get_service_metrics",
                    arguments={"service": "billing-service", "metric": "latency_p99"},
                )
            ]
        )
    )
    assert result["tool_calls"][0].arguments["start"] == WINDOW[0].isoformat()
    assert "latency_p99" in result["context"].metrics


async def test_a_failing_tool_does_not_abort_the_round(registry):
    node = make_execute_tool_node(registry, Guardrails())
    result = await node(
        _state(
            pending_requests=[
                ToolRequest(
                    tool="get_service_metrics",
                    arguments={"service": "nope", "metric": "error_rate"},
                ),
                ToolRequest(tool="get_recent_alerts", arguments={"service": "billing-service"}),
            ]
        )
    )
    assert len(result["tool_calls"]) == 2
    assert result["errors"][0].kind == "tool_failed"
    assert result["context"].alerts, "the second call still ran and still counted"


async def test_execution_does_not_mutate_the_context_it_was_given(registry):
    """LangGraph merges returned state; mutating in place hides what changed."""
    original = CollectedContext()
    node = make_execute_tool_node(registry, Guardrails())
    result = await node(
        _state(
            context=original,
            pending_requests=[
                ToolRequest(tool="get_recent_alerts", arguments={"service": "billing-service"})
            ],
        )
    )
    assert original.alerts == []
    assert result["context"] is not original


async def test_repetition_is_detected_across_loop_iterations(registry):
    """The executor is rebuilt each round, so history has to come from state."""
    request = ToolRequest(tool="get_recent_alerts", arguments={"service": "billing-service"})
    node = make_execute_tool_node(registry, Guardrails(max_identical_calls=1))
    prior = ToolCallRecord(
        tool="get_recent_alerts",
        arguments={
            "service": "billing-service",
            "start": WINDOW[0].isoformat(),
            "end": WINDOW[1].isoformat(),
        },
        started_at=WINDOW[0],
        duration_ms=1.0,
        ok=True,
    )
    result = await node(_state(tool_calls=[prior], pending_requests=[request]))
    assert "RepetitionLimitExceeded" in result["tool_calls"][0].error


async def test_write_tools_cannot_be_reached_from_the_investigation_loop(registry):
    """Read-only is enforced at execution, not by hoping the planner behaves."""
    node = make_execute_tool_node(registry, Guardrails())
    result = await node(_state(pending_requests=[ToolRequest(tool="create_issue")]))
    assert "UnknownToolError" in result["tool_calls"][0].error


# ── Оценка и маршрутизация ──────────────────────────────────────────────────


async def test_evaluation_notices_when_a_round_produced_nothing():
    state = _state(observations=[{"node": "execute_tool", "tool": "x", "ok": False}])
    result = await evaluate_observation_node(state)
    assert result["loop_iterations"] == 1
    assert result["observations"][0]["produced_evidence"] is False


def test_loop_continues_only_while_it_is_making_progress():
    route = make_route_after_evaluation(Guardrails(max_tool_calls=20, max_workflow_steps=40))
    progressing = _state(
        loop_iterations=1,
        observations=[{"node": "evaluate_observation", "produced_evidence": True}],
    )
    assert route(progressing) == "select_tool"

    stalled = _state(
        loop_iterations=1,
        observations=[{"node": "evaluate_observation", "produced_evidence": False}],
    )
    assert route(stalled) == "generate_analysis"


def test_loop_stops_at_the_iteration_ceiling_whatever_else_is_true():
    route = make_route_after_evaluation(Guardrails(max_tool_calls=999, max_workflow_steps=999))
    state = _state(
        loop_iterations=MAX_LOOP_ITERATIONS,
        observations=[{"node": "evaluate_observation", "produced_evidence": True}],
    )
    assert route(state) == "generate_analysis"


@pytest.mark.parametrize(
    ("policy", "state_overrides"),
    [
        (Guardrails(max_tool_calls=6), {"tool_call_count": 6}),
        (Guardrails(max_workflow_steps=4), {"step_count": 4}),
    ],
)
def test_every_budget_independently_ends_the_loop(policy, state_overrides):
    route = make_route_after_evaluation(policy)
    state = _state(
        loop_iterations=1,
        observations=[{"node": "evaluate_observation", "produced_evidence": True}],
        **state_overrides,
    )
    assert route(state) == "generate_analysis"


async def test_the_heuristic_planner_terminates_the_loop_on_its_own(registry):
    """Not a budget test: the rule set has to run out of gaps by itself."""
    from app.adapters.mock.dataset import BILLING_5XX

    context = CollectedContext(
        alerts=list(BILLING_5XX.alerts),
        metrics={"latency_p99": BILLING_5XX.metrics[("billing-service", "latency_p99")]},
        commits=list(BILLING_5XX.commits),
    )
    node = make_select_tool_node(HeuristicPlanner(), registry, Guardrails())
    result = await node(_state(context=context))
    assert result["pending_requests"] == []
