"""What the planner may decide, and what it may not."""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage

from app.agent.llm import ScriptedChatModel
from app.agent.planner import HeuristicPlanner, LLMPlanner, render_briefing
from app.agent.state import CollectedContext, ToolCallRecord
from app.agent.tools.catalog import build_registry
from app.domain.models import Alert, AlertSeverity, ErrorGroup, Evidence, EvidenceKind

NOW = datetime(2026, 3, 17, 14, 40, tzinfo=UTC)


def _state(**overrides):
    base = {
        "task": "billing-service 5xx after the release",
        "target_service": "billing-service",
        "window_start": datetime(2026, 3, 17, 14, 0, tzinfo=UTC),
        "window_end": datetime(2026, 3, 17, 15, 0, tzinfo=UTC),
        "context": CollectedContext(),
        "evidence": [],
        "hypotheses": [],
        "tool_calls": [],
    }
    return base | overrides


def _tools(monitoring, code, logs):
    return list(build_registry(monitoring, code, logs))


def _alert() -> Alert:
    return Alert(
        name="HighErrorRate",
        service="billing-service",
        severity=AlertSeverity.CRITICAL,
        fired_at=NOW,
    )


def _error_group() -> ErrorGroup:
    return ErrorGroup(
        error_type="TypeError",
        count=42,
        first_seen=NOW,
        last_seen=NOW,
        sample_message="boom",
        stack_top="billing/charge.py:184",
    )


# ── Эвристический планировщик ───────────────────────────────────────────────


async def test_heuristic_asks_for_the_first_missing_signal(monitoring, code, logs):
    plan = await HeuristicPlanner().plan(_state(), _tools(monitoring, code, logs))
    assert [r.tool for r in plan.requests] == ["get_recent_alerts"]
    assert plan.requests[0].reason


async def test_heuristic_moves_on_once_a_gap_is_closed(monitoring, code, logs):
    context = CollectedContext(alerts=[_alert()])
    plan = await HeuristicPlanner().plan(_state(context=context), _tools(monitoring, code, logs))
    assert plan.requests[0].tool == "get_service_metrics"
    assert plan.requests[0].arguments["metric"] == "latency_p99"


async def test_heuristic_reads_code_only_once_errors_are_grounded(monitoring, code, logs):
    """Fetching commits before there is a failure to explain is a wasted call."""
    from app.domain.models import MetricSeries

    full = CollectedContext(
        alerts=[_alert()],
        metrics={
            "latency_p99": MetricSeries(
                service="billing-service", metric="latency_p99", unit="s", points=()
            )
        },
    )
    plan = await HeuristicPlanner().plan(_state(context=full), _tools(monitoring, code, logs))
    assert plan.is_done

    full.error_groups = [_error_group()]
    plan = await HeuristicPlanner().plan(_state(context=full), _tools(monitoring, code, logs))
    assert plan.requests[0].tool == "get_commits"


async def test_heuristic_stops_when_nothing_is_missing(monitoring, code, logs):
    from app.adapters.mock.dataset import BILLING_5XX

    context = CollectedContext(
        alerts=[_alert()],
        metrics={"latency_p99": BILLING_5XX.metrics[("billing-service", "latency_p99")]},
        error_groups=[_error_group()],
        commits=list(BILLING_5XX.commits),
    )
    plan = await HeuristicPlanner().plan(_state(context=context), _tools(monitoring, code, logs))
    assert plan.is_done
    assert "has been queried" in plan.rationale


async def test_heuristic_never_asks_for_a_tool_it_was_not_offered(monitoring, code, logs):
    """The offered set is the guardrail's output; the planner must respect it."""
    only_commits = [t for t in _tools(monitoring, code, logs) if t.name == "get_commits"]
    plan = await HeuristicPlanner().plan(_state(), only_commits)
    assert plan.is_done, "no gap that get_commits alone can close"


async def test_heuristic_gives_up_without_a_target_service(monitoring, code, logs):
    plan = await HeuristicPlanner().plan(
        _state(target_service=None), _tools(monitoring, code, logs)
    )
    assert plan.is_done
    assert "no target service" in plan.rationale


# ── LLM-планировщик ─────────────────────────────────────────────────────────


async def test_llm_planner_turns_tool_calls_into_requests(monitoring, code, logs):
    model = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_pull_request",
                        "args": {"service": "billing-service", "number": 482},
                        "id": "1",
                    }
                ],
            )
        ]
    )
    plan = await LLMPlanner(model).plan(_state(), _tools(monitoring, code, logs))
    assert [r.tool for r in plan.requests] == ["get_pull_request"]
    assert plan.requests[0].arguments["number"] == 482


async def test_llm_planner_treats_a_text_reply_as_done(monitoring, code, logs):
    model = ScriptedChatModel(responses=[AIMessage(content="The evidence is sufficient.")])
    plan = await LLMPlanner(model).plan(_state(), _tools(monitoring, code, logs))
    assert plan.is_done
    assert plan.rationale == "The evidence is sufficient."


async def test_llm_planner_caps_how_much_one_turn_may_request(monitoring, code, logs):
    """One confused turn must not be able to spend the whole budget."""
    calls = [
        {"name": "get_recent_alerts", "args": {"service": "billing-service"}, "id": str(i)}
        for i in range(6)
    ]
    model = ScriptedChatModel(responses=[AIMessage(content="", tool_calls=calls)])
    plan = await LLMPlanner(model, max_requests=2).plan(_state(), _tools(monitoring, code, logs))
    assert len(plan.requests) == 2


async def test_llm_planner_failure_is_reported_not_raised(monitoring, code, logs):
    model = ScriptedChatModel(responses=[])  # ответы заканчиваются сразу
    plan = await LLMPlanner(model).plan(_state(), _tools(monitoring, code, logs))
    assert plan.is_done
    assert plan.error is not None


async def test_llm_planner_is_offered_only_the_allowed_tools(monitoring, code, logs):
    model = ScriptedChatModel(responses=[AIMessage(content="done")])
    allowed = [t for t in _tools(monitoring, code, logs) if t.name == "get_commits"]
    await LLMPlanner(model).plan(_state(), allowed)
    assert [t["name"] for t in model.bound_tools] == ["get_commits"]


# ── Briefing ─────────────────────────────────────────────────────────────────


def test_briefing_lists_evidence_hypotheses_and_spent_calls(monitoring, code, logs):
    state = _state(
        evidence=[
            Evidence(
                kind=EvidenceKind.METRIC,
                summary="error_rate peaked at 0.11",
                source_tool="get_service_metrics",
                reference="billing-service/error_rate",
            )
        ],
        tool_calls=[
            ToolCallRecord(
                tool="get_recent_alerts",
                arguments={"service": "billing-service", "start": "2026-03-17T14:00:00+00:00"},
                started_at=NOW,
                duration_ms=1.0,
                ok=True,
            )
        ],
        tool_budget_remaining=5,
    )
    briefing = render_briefing(state, _tools(monitoring, code, logs))

    assert "error_rate peaked at 0.11" in briefing
    assert "do not repeat these" in briefing
    assert "Tool calls remaining in this run: 5" in briefing
    assert "get_recent_alerts(service=billing-service)" in briefing
    assert "2026-03-17T14:00:00" not in briefing, "timestamps in call history are noise"


def test_briefing_is_built_from_state_not_a_message_transcript(monitoring, code, logs):
    """Two identical states must produce identical briefings however they were
    reached — that is what keeps the prompt bounded as the loop iterates."""
    tools = _tools(monitoring, code, logs)
    assert render_briefing(_state(), tools) == render_briefing(_state(), tools)


def test_briefing_says_when_nothing_has_been_found_yet(monitoring, code, logs):
    assert "- (none)" in render_briefing(_state(), _tools(monitoring, code, logs))
