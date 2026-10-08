"""Graph assembly.

The shape of the workflow is the design document. Reading it should answer
"what can this agent do, and what can it not do" without reading a prompt::

    START
      ↓
    analyze_task
      ↓
    collect_initial_context ──(no usable signal)──→ insufficient_context ──→ END
      ↓
    correlate
      ↓
    select_tool ──(nothing more to ask)────────────────────┐
      ↓                                                    │
    execute_tool                                           │
      ↓                                                    ↓
    evaluate_observation ──(more to learn)──→ select_tool  │
      └──(enough / out of budget)─────────────────────────→ generate_analysis
                                                             ↓
                                                        propose_action
                                                             ↓
                        ┌──(nothing worth writing)───────────┤
                        ↓                                    ↓ (write proposed)
                  final_response ←──(rejected)──────── request_approval
                        ↑                              ** graph pauses here **
                        │                                    ↓ (approved)
                        └──────────────────────────── execute_action
                        ↓
                       END

Three properties hold by construction, not by instruction:

*The deterministic work happens first.* Baseline collection and correlation
run before the model is consulted at all, so the planner reasons about facts
rather than deciding how to find them.

*Every cycle has an exit that the model does not control.* The loop back to
``select_tool`` is guarded by a routing function that checks budgets and
progress; the planner can only ever shorten the loop, never extend it.

*The model is optional.* Pass no chat model and every node still runs, using
its deterministic counterpart. That is what makes the graph testable and what
makes a provider outage a degradation rather than an outage.

*Nothing outside the system changes without a person.* The only write in the
graph sits behind ``request_approval``, which interrupts execution. The pause
is durable — the checkpointer persists the state — so the decision is a
separate HTTP request from a separate human, not a callback held in memory.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.adapters.base import (
    CodeProvider,
    IssueProvider,
    KnowledgeProvider,
    LogProvider,
    MonitoringProvider,
)
from app.adapters.mock.providers import (
    MockCodeProvider,
    MockIssueProvider,
    MockLogProvider,
    MockMonitoringProvider,
)
from app.agent.guardrails import Guardrails
from app.agent.llm import build_chat_model
from app.agent.nodes.act import (
    make_execute_action_node,
    propose_action_node,
    request_approval_node,
    route_after_approval,
    route_after_proposal,
)
from app.agent.nodes.analyze_task import analyze_task_node
from app.agent.nodes.build_analysis import make_build_analysis_node
from app.agent.nodes.collect_context import make_collect_context_node
from app.agent.nodes.correlate import make_correlate_node
from app.agent.nodes.investigate import (
    evaluate_observation_node,
    make_execute_tool_node,
    make_route_after_evaluation,
    make_select_tool_node,
    route_after_selection,
)
from app.agent.planner import HeuristicPlanner, LLMPlanner, Planner
from app.agent.serde import agent_serializer
from app.agent.state import AgentState, ApprovalState, RunStatus
from app.agent.tools.catalog import build_registry
from app.services.cache import ToolCache


def run_config(run_id: str) -> dict[str, dict[str, str]]:
    """Checkpoint thread for one run.

    The run id is the thread id, so resuming after an approval resumes *that*
    investigation and cannot be pointed at another one by a caller who guesses
    a different identifier.
    """
    return {"configurable": {"thread_id": run_id}}


def has_enough_context(state: AgentState) -> Literal["correlate", "insufficient_context"]:
    """Gate between collection and analysis.

    Written as a pure function of state so routing can be unit-tested without
    running the graph, which is most of the value of keeping state explicit.
    """
    if state.get("status") is RunStatus.FAILED:
        return "insufficient_context"
    context = state.get("context")
    if context is None or not context.metrics:
        return "insufficient_context"
    return "correlate"


async def insufficient_context_node(state: AgentState) -> AgentState:
    reasons = [e.message for e in state.get("errors", [])] or [
        "no monitoring data was available for the requested service and window"
    ]
    return AgentState(
        current_step="insufficient_context",
        step_count=state.get("step_count", 0) + 1,
        status=RunStatus.FAILED,
        final_result=("The investigation stopped before analysis: " + "; ".join(reasons) + "."),
    )


async def finalize_node(state: AgentState) -> AgentState:
    """One sentence describing how the run ended, including what it did not do.

    A run that proposed an action and was refused must say so: silence would
    read as "nothing was worth doing", which is a different outcome.
    """
    analysis = state.get("analysis")
    summary = analysis.summary if analysis else "No analysis was produced."
    approval = state.get("approval_state")
    result = state.get("action_result") or {}

    if approval is ApprovalState.REJECTED:
        note = state.get("approval_note") or ""
        summary += " The proposed issue was not created: a reviewer declined it."
        summary += f" Reason given: {note}" if note else ""
    elif approval is ApprovalState.APPROVED and result.get("ok"):
        issue = (result.get("issue") or {}).get("key", "the issue")
        summary += f" Approved and filed as {issue}."
    elif approval is ApprovalState.APPROVED and not result.get("ok"):
        summary += " The approved action failed to execute; nothing was created."

    return AgentState(
        current_step="final_response",
        step_count=state.get("step_count", 0) + 1,
        status=RunStatus.FAILED if state.get("status") is RunStatus.FAILED else RunStatus.COMPLETED,
        final_result=summary,
    )


def build_graph(
    *,
    monitoring: MonitoringProvider | None = None,
    code: CodeProvider | None = None,
    logs: LogProvider | None = None,
    issues: IssueProvider | None = None,
    knowledge: KnowledgeProvider | None = None,
    model: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    planner: Planner | None = None,
    guardrails: Guardrails | None = None,
    cache: ToolCache | None = None,
    use_llm: bool = True,
    enable_issue_tools: bool = True,
):
    """Compile the workflow.

    Every collaborator is injectable because every one of them is something a
    test, an evaluation scenario or a deployment needs to substitute: mock or
    MCP-backed providers, a scripted or real model, a tighter policy.

    ``use_llm=False`` forces the deterministic path even when Ollama is
    available — the evaluation harness uses it as the baseline to measure the
    model-driven agent against.
    """
    monitoring = monitoring or MockMonitoringProvider()
    code = code or MockCodeProvider()
    logs = logs or MockLogProvider()
    if issues is None and enable_issue_tools:
        issues = MockIssueProvider()
    guardrails = guardrails or Guardrails()

    if use_llm and model is None:
        model = build_chat_model()
    if not use_llm:
        model = None
    planner = planner or (LLMPlanner(model) if model is not None else HeuristicPlanner())

    registry = build_registry(monitoring, code, logs, issues, knowledge)
    timeout = guardrails.tool_timeout_seconds

    builder = StateGraph(AgentState)
    builder.add_node("analyze_task", analyze_task_node)
    builder.add_node(
        "collect_initial_context",
        make_collect_context_node(monitoring, code, logs, timeout=timeout),
    )
    builder.add_node("correlate", make_correlate_node(code, timeout=timeout))
    builder.add_node("select_tool", make_select_tool_node(planner, registry, guardrails))
    builder.add_node("execute_tool", make_execute_tool_node(registry, guardrails, cache=cache))
    builder.add_node("evaluate_observation", evaluate_observation_node)
    builder.add_node("generate_analysis", make_build_analysis_node(model))
    builder.add_node("propose_action", propose_action_node)
    builder.add_node("request_approval", request_approval_node)
    builder.add_node("execute_action", make_execute_action_node(registry, guardrails))
    builder.add_node("insufficient_context", insufficient_context_node)
    builder.add_node("final_response", finalize_node)

    builder.add_edge(START, "analyze_task")
    builder.add_edge("analyze_task", "collect_initial_context")
    builder.add_conditional_edges(
        "collect_initial_context",
        has_enough_context,
        {"correlate": "correlate", "insufficient_context": "insufficient_context"},
    )
    builder.add_edge("correlate", "select_tool")
    builder.add_conditional_edges(
        "select_tool",
        route_after_selection,
        {"execute_tool": "execute_tool", "generate_analysis": "generate_analysis"},
    )
    builder.add_edge("execute_tool", "evaluate_observation")
    builder.add_conditional_edges(
        "evaluate_observation",
        make_route_after_evaluation(guardrails),
        {"select_tool": "select_tool", "generate_analysis": "generate_analysis"},
    )
    builder.add_edge("generate_analysis", "propose_action")
    builder.add_conditional_edges(
        "propose_action",
        route_after_proposal,
        {"request_approval": "request_approval", "final_response": "final_response"},
    )
    builder.add_conditional_edges(
        "request_approval",
        route_after_approval,
        {"execute_action": "execute_action", "final_response": "final_response"},
    )
    builder.add_edge("execute_action", "final_response")
    builder.add_edge("final_response", END)
    builder.add_edge("insufficient_context", END)

    # По умолчанию используется чекпоинтер в памяти, чтобы простой ``build_graph()``
    # поддерживал паузу. Приложение подставляет постоянное хранилище, благодаря
    # которому ожидание подтверждения переживает перезапуск.
    return builder.compile(checkpointer=checkpointer or InMemorySaver(serde=agent_serializer()))
