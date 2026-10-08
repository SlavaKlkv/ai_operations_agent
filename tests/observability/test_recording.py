"""Что агент сообщает о себе, и единственный счётчик, который не должен изменяться."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agent.state import ApprovalState, RunStatus, ToolCallRecord
from app.domain.models import Hypothesis, IncidentAnalysis
from app.observability import metrics, recording

NOW = datetime(2026, 3, 17, 14, 32, tzinfo=UTC)


def _value(metric, **labels) -> float:
    """Читает один образец из реестра или 0, если к нему никогда не обращались."""
    name = metric._name
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            matches_name = sample.name in (f"{name}_total", name, f"{name}_count")
            if matches_name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def _call(tool: str, ok: bool = True) -> ToolCallRecord:
    return ToolCallRecord(tool=tool, arguments={}, started_at=NOW, duration_ms=12.0, ok=ok)


def _state(**overrides):
    base = {
        "run_id": "run-1",
        "target_service": "billing-service",
        "status": RunStatus.COMPLETED,
        "tool_calls": [_call("get_service_metrics"), _call("get_commits", ok=False)],
        "tool_call_count": 2,
        "llm_calls": 2,
        "input_tokens": 900,
        "output_tokens": 120,
        "errors": [],
        "observations": [],
        "approval_state": ApprovalState.APPROVED,
        "analysis": IncidentAnalysis(
            service="billing-service",
            confidence=0.88,
            summary="Likely cause: v1.8.4.",
            suspected_causes=[Hypothesis(statement="v1.8.4", confidence=0.88)],
        ),
    }
    return base | overrides


def test_a_run_is_counted_by_service_and_outcome():
    before = _value(metrics.runs_finished, service="billing-service", status="completed")
    recording.record_run(_state(), duration_seconds=0.4)
    after = _value(metrics.runs_finished, service="billing-service", status="completed")
    assert after == before + 1


def test_tool_calls_are_counted_by_outcome_not_just_volume():
    before_ok = _value(metrics.tool_calls, tool="get_service_metrics", outcome="ok")
    before_err = _value(metrics.tool_calls, tool="get_commits", outcome="error")
    recording.record_run(_state(), duration_seconds=0.1)
    assert _value(metrics.tool_calls, tool="get_service_metrics", outcome="ok") == before_ok + 1
    assert _value(metrics.tool_calls, tool="get_commits", outcome="error") == before_err + 1


def test_token_usage_is_split_by_direction():
    before = _value(metrics.llm_tokens, direction="input")
    recording.record_run(_state(), duration_seconds=0.1)
    assert _value(metrics.llm_tokens, direction="input") == before + 900


def test_model_failures_are_visible_as_failures():
    """Деградировавший агент всё равно завершает запуски, поэтому отказ нужно где-то
    учитывать, иначе деградация незаметна."""
    from app.agent.state import RunError

    before = _value(metrics.llm_calls, outcome="error")
    state = _state(
        errors=[RunError(node="select_tool", kind="planner_failed", message="503")],
    )
    recording.record_run(state, duration_seconds=0.1)
    assert _value(metrics.llm_calls, outcome="error") == before + 1


@pytest.mark.parametrize(
    "approval", [ApprovalState.PENDING, ApprovalState.REJECTED, ApprovalState.NOT_REQUIRED]
)
def test_a_write_without_an_approval_increments_the_alarm(approval):
    """Именно по этому счётчику срабатывает оповещение, поэтому он выводится из
    зафиксированных фактов, а не берётся из флага."""
    before = _value(metrics.unapproved_writes, tool="create_issue")
    state = _state(tool_calls=[_call("create_issue")], approval_state=approval)
    recording.record_run(state, duration_seconds=0.1)
    assert _value(metrics.unapproved_writes, tool="create_issue") == before + 1


def test_an_approved_write_does_not_trip_the_alarm():
    before = _value(metrics.unapproved_writes, tool="create_issue")
    state = _state(tool_calls=[_call("create_issue")], approval_state=ApprovalState.APPROVED)
    recording.record_run(state, duration_seconds=0.1)
    assert _value(metrics.unapproved_writes, tool="create_issue") == before


def test_integration_health_is_reported_per_server():
    from app.mcp.client import ServerStatus

    recording.record_integration_health(
        [
            ServerStatus(name="monitoring", connected=True, required=True, tool_count=4),
            ServerStatus(name="knowledge", connected=False, required=False, error="down"),
        ],
        durable_checkpointer=False,
    )
    assert _value(metrics.mcp_server_up, server="monitoring", required="true") == 1
    assert _value(metrics.mcp_server_up, server="knowledge", required="false") == 0
    assert _value(metrics.checkpointer_durable) == 0


# ── Трасса ───────────────────────────────────────────────────────────────────


def test_the_trace_replays_the_nodes_in_order():
    state = _state(
        observations=[
            {"node": "analyze_task", "target_service": "billing-service"},
            {"node": "select_tool", "requested": ["get_commits"]},
            {"node": "execute_tool", "tool": "get_commits", "ok": True},
        ]
    )
    trace = recording.build_trace(state)
    assert [e.node for e in trace] == ["analyze_task", "select_tool", "execute_tool"]
    assert trace[0].step == 1
    assert trace[1].detail["requested"] == ["get_commits"]


def test_the_rendered_trace_ends_with_the_conclusion():
    state = _state(observations=[{"node": "correlate", "spike_factor": 29}])
    rendered = recording.render_trace(state)
    assert "correlate" in rendered
    assert "0.88 Likely cause: v1.8.4." in rendered


def test_long_values_are_truncated_so_a_trace_stays_readable():
    state = _state(observations=[{"node": "execute_tool", "summary": "x" * 500}])
    rendered = recording.render_trace(state)
    assert "…" in rendered
    assert len(max(rendered.splitlines(), key=len)) < 200


# ── HTTP ─────────────────────────────────────────────────────────────────────


async def test_the_metrics_endpoint_serves_prometheus_text(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "agent_runs_finished_total" in response.text
    assert "agent_unapproved_writes_total" in response.text


async def test_a_run_shows_up_in_the_trace_endpoint(client):
    task = "После последнего релиза billing-service резко выросло количество 5xx. Разберись."
    created = (await client.post("/runs", json={"task": task})).json()

    trace = (await client.get(f"/runs/{created['id']}/trace")).json()
    nodes = [s["node"] for s in trace["steps"]]
    assert "analyze_task" in nodes
    assert "correlate" in nodes
    assert "propose_action" in nodes
    assert trace["status"] == "awaiting_approval"
    assert any(c["tool"] == "get_service_metrics" for c in trace["tool_calls"])


async def test_the_trace_of_an_unknown_run_is_404(client):
    import uuid

    assert (await client.get(f"/runs/{uuid.uuid4()}/trace")).status_code == 404
