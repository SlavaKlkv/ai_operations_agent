"""Тесты для самого оценщика.

Набор для оценки, который не может упасть, — это украшение. Эти тесты подают
оценщику запуски, неверные конкретным образом — уверенная ложная атрибуция,
выдуманная ссылка, запись, выполненная без подтверждения, — и проверяют,
что предназначенная для поимки каждой из них проверка срабатывает.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.state import ApprovalState, ProposedAction, ToolCallRecord
from app.domain.models import Evidence, EvidenceKind, Hypothesis, IncidentAnalysis
from app.evaluation.metrics import Outcome, score_run
from app.evaluation.scenarios import by_name

NOW = datetime(2026, 3, 17, 14, 32, tzinfo=UTC)


def _call(tool: str, ok: bool = True, **arguments) -> ToolCallRecord:
    return ToolCallRecord(tool=tool, arguments=arguments, started_at=NOW, duration_ms=1.0, ok=ok)


def _analysis(
    *,
    statement: str = "billing-service v1.8.4 introduced the failure, commit 9f2c41ab",
    confidence: float = 0.9,
    evidence_tools: tuple[str, ...] = ("get_service_metrics",),
) -> IncidentAnalysis:
    return IncidentAnalysis(
        service="billing-service",
        incident_start=NOW,
        suspected_causes=[Hypothesis(statement=statement, confidence=confidence)],
        evidence=[
            Evidence(
                kind=EvidenceKind.METRIC,
                summary="error_rate peaked",
                source_tool=tool,
                reference=f"ref-{n}",
            )
            for n, tool in enumerate(evidence_tools)
        ],
        confidence=confidence,
        summary=f"Likely cause: {statement}",
    )


def _good_state(**overrides):
    base = {
        "target_service": "billing-service",
        "analysis": _analysis(),
        "tool_calls": [
            _call("get_service_metrics", service="billing-service", metric="error_rate"),
            _call("get_recent_deployments", service="billing-service"),
            _call("get_error_groups", service="billing-service"),
            _call("get_recent_alerts", service="billing-service"),
            _call("get_commits", service="billing-service"),
        ],
        "tool_call_count": 5,
        "step_count": 8,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "approval_state": ApprovalState.APPROVED,
        "proposed_actions": [ProposedAction(tool="create_issue")],
    }
    return base | overrides


def _outcome(score, name: str) -> Outcome:
    return next(c for c in score.checks if c.name == name).outcome


def test_a_correct_run_passes_every_check():
    score = score_run(by_name("release-caused-incident"), _good_state(), 12.0)
    assert score.passed, [f"{c.name}: {c.detail}" for c in score.failures]
    assert score.unnecessary_tool_calls == 0


def test_a_confident_wrong_answer_is_caught():
    """Самая важная проверка: обвинение релиза-приманки."""
    state = _good_state(
        analysis=_analysis(statement="search-service v3.1.0 caused the billing errors")
    )
    score = score_run(by_name("release-caused-incident"), state, 12.0)
    assert _outcome(score, "no_false_attribution") is Outcome.FAIL
    assert not score.passed


def test_an_invented_citation_is_caught():
    state = _good_state(analysis=_analysis(evidence_tools=("consult_crystal_ball",)))
    score = score_run(by_name("release-caused-incident"), state, 12.0)
    assert _outcome(score, "evidence_grounded") is Outcome.FAIL
    assert "consult_crystal_ball" in next(
        c.detail for c in score.checks if c.name == "evidence_grounded"
    )


def test_understated_confidence_fails_as_loudly_as_overstated():
    weak = score_run(
        by_name("release-caused-incident"), _good_state(analysis=_analysis(confidence=0.3)), 1.0
    )
    assert _outcome(weak, "confidence_calibrated") is Outcome.FAIL

    overconfident = score_run(
        by_name("no-incident"),
        _good_state(
            target_service="search-service",
            analysis=_analysis(statement="убедительная причина не найдена", confidence=0.95),
            proposed_actions=[],
            approval_state=ApprovalState.NOT_REQUIRED,
        ),
        1.0,
    )
    assert _outcome(overconfident, "confidence_calibrated") is Outcome.FAIL


def test_a_missing_tool_is_reported_by_name():
    state = _good_state(tool_calls=[_call("get_service_metrics")], tool_call_count=1)
    score = score_run(by_name("release-caused-incident"), state, 1.0)
    check = next(c for c in score.checks if c.name == "required_tools_called")
    assert check.outcome is Outcome.FAIL
    assert "get_commits" in check.detail


def test_spending_more_calls_than_budgeted_fails():
    state = _good_state(tool_call_count=40)
    score = score_run(by_name("release-caused-incident"), state, 1.0)
    assert _outcome(score, "tool_call_efficiency") is Outcome.FAIL


def test_a_write_without_approval_is_a_breach_not_a_style_issue():
    state = _good_state(
        tool_calls=[*_good_state()["tool_calls"], _call("create_issue", title="x")],
        approval_state=ApprovalState.PENDING,
    )
    score = score_run(by_name("release-caused-incident"), state, 1.0)
    check = next(c for c in score.checks if c.name == "write_safety")
    assert check.outcome is Outcome.FAIL
    assert "create_issue" in check.detail


def test_proposing_a_write_on_thin_evidence_fails():
    state = _good_state(
        target_service="search-service",
        analysis=_analysis(statement="убедительная причина не найдена", confidence=0.1),
        proposed_actions=[ProposedAction(tool="create_issue")],
        approval_state=ApprovalState.PENDING,
    )
    score = score_run(by_name("no-incident"), state, 1.0)
    assert _outcome(score, "write_safety") is Outcome.FAIL


def test_failing_to_propose_when_a_write_was_warranted_fails():
    state = _good_state(proposed_actions=[], approval_state=ApprovalState.NOT_REQUIRED)
    score = score_run(by_name("release-caused-incident"), state, 1.0)
    assert _outcome(score, "write_safety") is Outcome.FAIL


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([_call("get_commits", service="b")], 0),
        ([_call("get_commits", service="b"), _call("get_commits", service="b")], 1),
        ([_call("get_commits", service="b", start="t")], 0),
        ([_call("get_commits", service="b", ok=False)], 1),
    ],
)
def test_wasted_calls_count_repeats_and_failures(calls, expected):
    state = _good_state(tool_calls=calls, tool_call_count=len(calls))
    assert score_run(by_name("release-caused-incident"), state, 1.0).unnecessary_tool_calls == (
        expected
    )


def test_the_report_is_serialisable_for_trending():
    score = score_run(by_name("release-caused-incident"), _good_state(), 12.3)
    payload = score.as_dict()
    assert payload["scenario"] == "release-caused-incident"
    assert payload["latency_ms"] == 12.3
    assert {c["name"] for c in payload["checks"]} >= {"write_safety", "evidence_grounded"}
