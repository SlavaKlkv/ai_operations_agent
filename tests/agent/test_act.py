"""Предложение, подтверждение и выполнение записи — часть с последствиями."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langgraph.types import Command

from app.adapters.mock.providers import MockIssueProvider
from app.agent.graph import build_graph, run_config
from app.agent.guardrails import Guardrails
from app.agent.nodes.act import (
    PROPOSAL_CONFIDENCE_FLOOR,
    issue_body,
    make_execute_action_node,
    propose_action_node,
    route_after_approval,
    route_after_proposal,
)
from app.agent.state import ApprovalState, ProposedAction, RunStatus
from app.agent.tools.base import ToolAccess
from app.agent.tools.catalog import build_registry
from app.domain.models import Evidence, EvidenceKind, Hypothesis, IncidentAnalysis

NOW = datetime(2026, 3, 17, 14, 32, tzinfo=UTC)


def _analysis(confidence: float = 0.85) -> IncidentAnalysis:
    return IncidentAnalysis(
        service="billing-service",
        incident_start=NOW,
        symptoms=["error_rate rose to 11%"],
        suspected_causes=[Hypothesis(statement="v1.8.4 broke charging", confidence=confidence)],
        evidence=[
            Evidence(
                kind=EvidenceKind.METRIC,
                summary="error_rate peaked at 0.113 ratio",
                source_tool="get_service_metrics",
                reference="billing-service/error_rate",
            )
        ],
        confidence=confidence,
        recommended_actions=["Roll back to v1.8.3."],
        summary="Likely cause: v1.8.4.",
    )


def _state(**overrides):
    base = {
        "run_id": "run-1",
        "task": "billing-service 5xx",
        "target_service": "billing-service",
        "analysis": _analysis(),
        "hypotheses": [
            Hypothesis(
                statement="v1.8.4 broke charging",
                confidence=0.85,
                supporting_evidence=("billing-service/error_rate",),
            )
        ],
        "tool_calls": [],
        "errors": [],
        "observations": [],
        "step_count": 5,
        "tool_call_count": 7,
        "proposed_actions": [],
        "approval_state": ApprovalState.NOT_REQUIRED,
    }
    return base | overrides


# ── propose_action ───────────────────────────────────────────────────────────


async def test_a_confident_analysis_produces_a_reviewable_proposal():
    result = await propose_action_node(_state())
    action = result["proposed_actions"][0]

    assert action.tool == "create_issue"
    assert action.requires_approval is True
    assert result["approval_state"] is ApprovalState.PENDING
    assert "billing-service" in action.arguments["title"]
    assert action.rationale


async def test_a_low_confidence_analysis_proposes_nothing():
    """Issue, заведённый по догадке, стоит кому-то расследования, чтобы его опровергнуть."""
    below = PROPOSAL_CONFIDENCE_FLOOR - 0.1
    result = await propose_action_node(_state(analysis=_analysis(below)))

    assert result["proposed_actions"] == []
    assert result["approval_state"] is ApprovalState.NOT_REQUIRED
    assert "must both reach" in result["observations"][0]["reason"]


async def test_a_model_confidence_cannot_override_a_weak_deterministic_hypothesis():
    result = await propose_action_node(
        _state(
            analysis=_analysis(0.95),
            hypotheses=[Hypothesis(statement="only a weak lead", confidence=0.3)],
        )
    )

    assert result["proposed_actions"] == []
    assert result["approval_state"] is ApprovalState.NOT_REQUIRED
    assert "deterministic hypothesis 0.30" in result["observations"][0]["reason"]


async def test_no_analysis_means_no_proposal():
    result = await propose_action_node(_state(analysis=None))
    assert result["proposed_actions"] == []


def test_routing_sends_only_writes_to_a_human():
    assert route_after_proposal({"proposed_actions": []}) == "final_response"
    assert (
        route_after_proposal(
            {"proposed_actions": [ProposedAction(tool="x", requires_approval=False)]}
        )
        == "final_response"
    )
    assert (
        route_after_proposal({"proposed_actions": [ProposedAction(tool="create_issue")]})
        == "request_approval"
    )


def test_routing_treats_a_rejection_as_a_normal_ending():
    assert route_after_approval({"approval_state": ApprovalState.APPROVED}) == "execute_action"
    assert route_after_approval({"approval_state": ApprovalState.REJECTED}) == "final_response"
    assert route_after_approval({}) == "final_response"


# ── Текст, который подтверждает человек ─────────────────────────────────────


def test_the_issue_body_carries_the_evidence_and_its_provenance():
    body = issue_body(_analysis(), _state())

    assert "## Summary" in body
    assert "## Evidence" in body
    assert "`get_service_metrics`" in body, "each claim names the tool that produced it"
    assert "## Recommended actions" in body
    assert "run `run-1`" in body
    assert "approved by a human" in body


def test_the_body_admits_what_could_not_be_collected():
    from app.agent.state import RunError

    failure = RunError(node="execute_tool", kind="tool_failed", message="get_commits: timeout")
    body = issue_body(_analysis(), _state(errors=[failure]))
    assert "could not be collected" in body
    assert "get_commits: timeout" in body


def test_the_body_is_bounded():
    """Issue читает человек; свалка логов — не отчёт об инциденте."""
    huge = _analysis().model_copy(update={"symptoms": ["x" * 500] * 200})
    assert len(issue_body(huge, _state())) <= 20_000


# ── execute_action ───────────────────────────────────────────────────────────


@pytest.fixture
def registry(monitoring, code, logs):
    return build_registry(monitoring, code, logs, MockIssueProvider())


async def test_an_approved_action_runs_and_records_its_effect(registry):
    node = make_execute_action_node(registry, Guardrails())
    result = await node(
        _state(
            approval_state=ApprovalState.APPROVED,
            approved_by="oncall@example.com",
            proposed_actions=[
                ProposedAction(
                    tool="create_issue",
                    arguments={"title": "Elevated errors", "body": "b" * 50},
                )
            ],
        )
    )
    assert result["action_result"]["ok"] is True
    assert result["action_result"]["issue"]["key"].startswith("OPS-")
    assert result["tool_calls"][0].tool == "create_issue"
    assert result["tool_calls"][0].ok
    assert result["status"] is RunStatus.RUNNING


@pytest.mark.parametrize(
    "approval",
    [ApprovalState.PENDING, ApprovalState.REJECTED, ApprovalState.NOT_REQUIRED],
)
async def test_the_write_step_refuses_without_an_approval(registry, approval):
    """Эшелонированная защита: маршрутизация уже предотвращает это, но проверка остаётся."""
    node = make_execute_action_node(registry, Guardrails())
    result = await node(
        _state(
            approval_state=approval,
            proposed_actions=[ProposedAction(tool="create_issue", arguments={})],
        )
    )
    assert result["errors"][0].kind == "not_approved"
    assert "tool_calls" not in result


async def test_the_write_permission_covers_only_the_approved_tool(registry):
    """Подтверждение issue не открывает заодно и комментирование."""
    node = make_execute_action_node(registry, Guardrails())
    result = await node(
        _state(
            approval_state=ApprovalState.APPROVED,
            proposed_actions=[
                ProposedAction(
                    tool="add_issue_comment", arguments={"key": "OPS-1", "text": "note"}
                ),
                ProposedAction(
                    tool="create_issue", arguments={"title": "x" * 10, "body": "y" * 50}
                ),
            ],
        )
    )
    # Выполняется только первое предложение, и политика сужается до него.
    assert result["tool_calls"][0].tool == "add_issue_comment"
    assert result["tool_calls"][0].ok


async def test_a_failed_write_fails_the_run_rather_than_reporting_success(registry):
    node = make_execute_action_node(registry, Guardrails())
    result = await node(
        _state(
            approval_state=ApprovalState.APPROVED,
            proposed_actions=[
                ProposedAction(tool="create_issue", arguments={"title": "no", "body": "short"})
            ],
        )
    )
    assert result["status"] is RunStatus.FAILED
    assert result["errors"][0].kind == "write_failed"
    assert result["action_result"]["ok"] is False


async def test_write_tools_stay_invisible_to_the_investigation_loop(registry):
    """Реестр их содержит; политика по умолчанию их не открывает."""
    policy = Guardrails()
    offered = {t.name for t in policy.available(registry)}
    assert "create_issue" not in offered
    assert {t.name for t in registry.by_access(ToolAccess.WRITE)} == {
        "create_issue",
        "add_issue_comment",
    }


# ── Сквозной сценарий ────────────────────────────────────────────────────────


async def test_the_approved_content_is_what_was_shown(monitoring, code, logs, fresh_state):
    """Ревьюер видит тело issue; возобновление несёт только решение,
    поэтому заведённое не может отличаться от одобренного."""
    issues = MockIssueProvider()
    graph = build_graph(monitoring=monitoring, code=code, logs=logs, issues=issues, use_llm=False)
    config = run_config(fresh_state["run_id"])

    paused = await graph.ainvoke(fresh_state, config)
    shown = paused["__interrupt__"][0].value["arguments"]

    final = await graph.ainvoke(
        Command(resume={"approved": True, "decided_by": "oncall@example.com"}), config
    )

    created = next(i for i in issues.issues if i.key == final["action_result"]["issue"]["key"])
    assert created.title == shown["title"]
    assert created.body == shown["body"]
    assert created.created_by == "ai-operations-agent"


async def test_a_rejected_run_leaves_the_tracker_untouched(monitoring, code, logs, fresh_state):
    issues = MockIssueProvider()
    before = len(issues.issues)
    graph = build_graph(monitoring=monitoring, code=code, logs=logs, issues=issues, use_llm=False)
    config = run_config(fresh_state["run_id"])

    await graph.ainvoke(fresh_state, config)
    final = await graph.ainvoke(
        Command(resume={"approved": False, "decided_by": "sre@example.com", "note": "duplicate"}),
        config,
    )

    assert len(issues.issues) == before
    assert final["approval_state"] is ApprovalState.REJECTED
    assert final["status"] is RunStatus.COMPLETED
    assert "duplicate" in final["final_result"]


async def test_resuming_reads_the_run_from_the_checkpoint_not_from_the_caller(
    monitoring, code, logs, fresh_state
):
    """Возобновление несёт только решение — ни задачи, ни анализа,
    ни предложения. Всё, что нужно записи, должно вернуться из
    чекпоинта, и именно это делает паузу переживающей запросы."""
    issues = MockIssueProvider()
    config = run_config(fresh_state["run_id"])
    graph = build_graph(monitoring=monitoring, code=code, logs=logs, issues=issues, use_llm=False)

    await graph.ainvoke(fresh_state, config)
    resumed = await graph.ainvoke(
        Command(resume={"approved": True, "decided_by": "oncall@example.com"}), config
    )

    assert resumed["task"] == fresh_state["task"]
    assert resumed["analysis"] is not None
    assert resumed["tool_call_count"] > 1, "the investigation's history came back too"
    assert resumed["action_result"]["ok"] is True


async def test_a_second_run_gets_its_own_thread(monitoring, code, logs):
    """Два расследования не должны возобновляться друг в друга."""
    from app.agent.state import initial_state

    issues = MockIssueProvider()
    graph = build_graph(monitoring=monitoring, code=code, logs=logs, issues=issues, use_llm=False)
    task = "billing-service 5xx после релиза"

    first = await graph.ainvoke(initial_state("run-a", task), run_config("run-a"))
    second = await graph.ainvoke(initial_state("run-b", task), run_config("run-b"))
    assert first["__interrupt__"] and second["__interrupt__"]

    await graph.ainvoke(
        Command(resume={"approved": True, "decided_by": "a@example.com"}), run_config("run-a")
    )
    # Подтверждение run-a не должно снимать ожидание с run-b.
    still_waiting = await graph.aget_state(run_config("run-b"))
    assert still_waiting.interrupts
