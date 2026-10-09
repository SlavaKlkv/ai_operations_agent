"""Тесты уровня узлов: каждый узел — обычная асинхронная функция от состояния,
поэтому его можно прогнать без компиляции графа."""

from __future__ import annotations

from datetime import timedelta

from app.agent.nodes.analyze_task import analyse_task, analyze_task_node, extract_service
from app.agent.nodes.build_analysis import build_analysis_node
from app.agent.nodes.collect_context import make_collect_context_node
from app.agent.nodes.correlate import make_correlate_node
from app.agent.state import AgentState, RunStatus
from app.domain.models import EvidenceKind


def test_service_extraction():
    assert extract_service("billing-service вырос 5xx") == "billing-service"
    assert extract_service("api-gateway-service is slow") == "api-gateway-service"
    assert extract_service("everything is on fire") is None


def test_task_analysis_produces_a_bounded_window():
    analysis = analyse_task("5xx in billing-service")
    assert analysis.target_service == "billing-service"
    assert analysis.window_end - analysis.window_start == timedelta(hours=1)
    assert "5xx" in analysis.keywords


async def test_analyze_task_node_writes_service_and_window(fresh_state):
    update = await analyze_task_node(fresh_state)
    assert update["target_service"] == "billing-service"
    assert update["step_count"] == 1
    assert update["window_start"] < update["window_end"]


async def test_collect_context_gathers_all_baseline_signals(fresh_state, monitoring, code, logs):
    state = {**fresh_state, **await analyze_task_node(fresh_state)}
    node = make_collect_context_node(monitoring, code, logs)
    update = await node(state)

    assert update["tool_call_count"] == 6  # 3 метрики + деплои + логи + оповещения
    assert set(update["context"].metrics) == {"error_rate", "latency_p99", "request_rate"}
    assert update["context"].deployments and update["context"].error_groups
    kinds = {e.kind for e in update["evidence"]}
    assert kinds == {
        EvidenceKind.METRIC,
        EvidenceKind.DEPLOYMENT,
        EvidenceKind.LOG,
        EvidenceKind.ALERT,
    }
    assert update["errors"] == []


async def test_collect_context_fails_loudly_without_a_target(fresh_state, monitoring, code, logs):
    node = make_collect_context_node(monitoring, code, logs)
    update = await node({**fresh_state, "target_service": None})
    assert update["status"] is RunStatus.FAILED
    assert update["errors"][0].kind == "insufficient_input"
    assert update["errors"][0].recoverable is False


async def test_correlate_blames_the_release_that_preceded_the_spike(
    fresh_state, monitoring, code, logs
):
    state = {**fresh_state, **await analyze_task_node(fresh_state)}
    state = {**state, **await make_collect_context_node(monitoring, code, logs)(state)}
    update = await make_correlate_node(code)(state)

    hypothesis = update["hypotheses"][0]
    assert "v1.8.4" in hypothesis.statement
    assert "v3.1.0" not in hypothesis.statement  # ложный релиз не должен стать причиной
    assert hypothesis.confidence >= 0.8
    assert any(e.kind is EvidenceKind.COMMIT for e in update["evidence"])


async def test_correlate_reports_no_signal_on_a_flat_service(fresh_state, monitoring, code, logs):
    state = {**fresh_state, "target_service": "search-service"}
    state = {**state, **await analyze_task_node(state)}
    state["target_service"] = "search-service"
    collect = make_collect_context_node(monitoring, code, logs)
    # У search-service есть только ряд error_rate; сбой остальных рядов допустим.
    state = {**state, **await collect(state)}
    update = await make_correlate_node(code)(state)
    assert update["hypotheses"] == []
    assert update["errors"][0].kind == "no_signal"


async def test_analysis_refuses_to_recommend_action_without_confidence(fresh_state):
    """Слабая гипотеза должна давать "посмотреть дальше", а не приказ об откате."""
    from app.domain.models import Hypothesis

    state: AgentState = {
        **fresh_state,
        "target_service": "billing-service",
        "hypotheses": [Hypothesis(statement="maybe the moon", confidence=0.2)],
    }
    update = await build_analysis_node(state)
    analysis = update["analysis"]
    assert analysis.confidence == 0.2
    assert analysis.requires_human_review is True
    assert "Откатите" not in " ".join(analysis.recommended_actions)
