"""Graph-level tests: routing decisions and a full deterministic run."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from app.adapters.mock.dataset import INCIDENT_START
from app.agent.graph import build_graph, has_enough_context, run_config
from app.agent.guardrails import Guardrails
from app.agent.llm import ScriptedChatModel
from app.agent.nodes.investigate import MAX_LOOP_ITERATIONS
from app.agent.state import CollectedContext, RunStatus, initial_state
from app.domain.models import MetricSeries


def test_routing_requires_metrics():
    assert has_enough_context({"context": CollectedContext()}) == "insufficient_context"
    assert has_enough_context({}) == "insufficient_context"


def test_routing_short_circuits_on_failure():
    context = CollectedContext(
        metrics={
            "error_rate": MetricSeries(service="s", metric="error_rate", unit="ratio", points=())
        }
    )
    assert has_enough_context({"context": context}) == "correlate"
    assert (
        has_enough_context({"context": context, "status": RunStatus.FAILED})
        == "insufficient_context"
    )


async def test_full_run_identifies_the_release(monitoring, code, logs, fresh_state):
    """A confident run does not finish on its own: it stops to ask."""
    graph = build_graph(monitoring=monitoring, code=code, logs=logs)
    final = await graph.ainvoke(fresh_state, run_config(fresh_state["run_id"]))

    assert final["__interrupt__"], "a write was proposed, so the graph must pause"
    assert final["current_step"] == "propose_action", (
        "the pause happens inside request_approval, so the last completed step "
        "is the one that produced the proposal"
    )
    analysis = final["analysis"]
    assert analysis.service == "billing-service"
    assert analysis.incident_start == INCIDENT_START
    assert "v1.8.4" in analysis.suspected_causes[0].statement
    assert analysis.confidence >= 0.8
    assert analysis.requires_human_review is True


async def test_every_claim_is_backed_by_a_tool_call(monitoring, code, logs, fresh_state):
    """Evidence grounding: no evidence item may cite a tool that never ran."""
    graph = build_graph(monitoring=monitoring, code=code, logs=logs)
    final = await graph.ainvoke(fresh_state, run_config(fresh_state["run_id"]))

    executed = {r.tool for r in final["tool_calls"]} | {"detect_spike"}
    cited = {e.source_tool for e in final["analysis"].evidence}
    assert cited <= executed


async def test_run_stays_within_its_budget(monitoring, code, logs, fresh_state):
    graph = build_graph(monitoring=monitoring, code=code, logs=logs)
    final = await graph.ainvoke(fresh_state, run_config(fresh_state["run_id"]))
    assert final["tool_call_count"] <= 12
    assert final["step_count"] <= 30


async def test_unknown_service_ends_in_a_stated_failure(monitoring, code, logs):
    graph = build_graph(monitoring=monitoring, code=code, logs=logs)
    final = await graph.ainvoke(
        initial_state("r1", "что-то не так с payments-service"), run_config("r1")
    )

    assert final["status"] is RunStatus.FAILED
    assert final["current_step"] == "insufficient_context"
    assert "stopped before analysis" in final["final_result"]
    assert final["analysis"] is None


async def test_failure_path_records_why(monitoring, code, logs):
    graph = build_graph(monitoring=monitoring, code=code, logs=logs)
    final = await graph.ainvoke(initial_state("r2", "no service named here"), run_config("r2"))
    assert final["errors"]
    assert final["errors"][0].kind == "insufficient_input"


# ── Запуски под управлением модели ──────────────────────────────────────────


def _draft_call(**overrides):
    args = {
        "summary": "billing-service v1.8.4 broke invoice charging.",
        "symptoms": ["5xx rose from 0.4% to 11%"],
        "suspected_causes": [
            {
                "statement": "The regional tax change shipped in v1.8.4 raises on some invoices.",
                "confidence": 0.88,
                "supporting_evidence": ["error_rate"],
            }
        ],
        "recommended_actions": ["Roll back billing-service to v1.8.3."],
        "confidence": 0.88,
    } | overrides
    return AIMessage(content="", tool_calls=[{"name": "AnalysisDraft", "args": args, "id": "d"}])


async def test_the_model_can_add_a_tool_call_and_then_conclude(monitoring, code, logs, fresh_state):
    """The full cycle: plan → execute → evaluate → plan again → analyse."""
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
            ),
            AIMessage(content="The pull request confirms the change; evidence is sufficient."),
            _draft_call(),
        ]
    )
    final = await build_graph(monitoring=monitoring, code=code, logs=logs, model=model).ainvoke(
        fresh_state, run_config(fresh_state["run_id"])
    )

    assert final["__interrupt__"]
    assert final["loop_iterations"] == 1
    assert "get_pull_request" in [c.tool for c in final["tool_calls"]]
    assert final["analysis"].summary.startswith("billing-service v1.8.4")
    assert final["llm_calls"] == 3


async def test_a_model_that_cites_evidence_it_never_saw_loses_the_citation(
    monitoring, code, logs, fresh_state
):
    """Grounding is enforced structurally: invented references are dropped and
    the hypothesis is demoted rather than being taken at its word."""
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="Enough."),
            _draft_call(
                suspected_causes=[
                    {
                        "statement": "A database migration corrupted the invoice table.",
                        "confidence": 0.95,
                        "supporting_evidence": ["migration-log-42"],
                    }
                ]
            ),
        ]
    )
    final = await build_graph(monitoring=monitoring, code=code, logs=logs, model=model).ainvoke(
        fresh_state, run_config(fresh_state["run_id"])
    )

    cause = final["analysis"].suspected_causes[0]
    assert cause.supporting_evidence == ()
    assert cause.confidence <= 0.4
    assert final["analysis"].confidence <= 0.4


async def test_the_evidence_list_is_never_authored_by_the_model(
    monitoring, code, logs, fresh_state
):
    model = ScriptedChatModel(responses=[AIMessage(content="Enough."), _draft_call()])
    final = await build_graph(monitoring=monitoring, code=code, logs=logs, model=model).ainvoke(
        fresh_state, run_config(fresh_state["run_id"])
    )

    executed = {r.tool for r in final["tool_calls"]} | {"detect_spike"}
    assert {e.source_tool for e in final["analysis"].evidence} <= executed


async def test_a_model_outage_degrades_the_run_instead_of_failing_it(
    monitoring, code, logs, fresh_state
):
    """Both model calls fail. The run must still produce a grounded analysis."""
    model = ScriptedChatModel(responses=[])
    final = await build_graph(monitoring=monitoring, code=code, logs=logs, model=model).ainvoke(
        fresh_state, run_config(fresh_state["run_id"])
    )

    assert final["analysis"] is not None
    assert "v1.8.4" in final["analysis"].suspected_causes[0].statement
    assert {e.kind for e in final["errors"]} == {"planner_failed", "llm_failed"}


async def test_a_looping_model_is_stopped_by_the_iteration_ceiling(
    monitoring, code, logs, fresh_state
):
    """A planner that always wants one more call must still terminate."""
    keeps_asking = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_service_metrics",
                    "args": {"service": "billing-service", "metric": metric},
                    "id": str(i),
                }
            ],
        )
        for i, metric in enumerate(
            ["latency_p50", "latency_p95", "request_rate", "error_rate", "latency_p99"] * 3
        )
    ]
    model = ScriptedChatModel(responses=[*keeps_asking, _draft_call()])
    final = await build_graph(
        monitoring=monitoring,
        code=code,
        logs=logs,
        model=model,
        guardrails=Guardrails(max_tool_calls=12),
    ).ainvoke(fresh_state, run_config(fresh_state["run_id"]))

    assert final["loop_iterations"] <= MAX_LOOP_ITERATIONS
    assert final["tool_call_count"] <= 12


async def test_use_llm_false_forces_the_deterministic_baseline(monitoring, code, logs, fresh_state):
    """The evaluation harness needs a baseline that ignores configuration."""
    final = await build_graph(monitoring=monitoring, code=code, logs=logs, use_llm=False).ainvoke(
        fresh_state, run_config(fresh_state["run_id"])
    )
    assert final["llm_calls"] == 0
    assert final["analysis"] is not None
