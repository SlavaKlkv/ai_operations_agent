"""Running the evaluation suite and reporting what it found.

Each scenario gets a freshly built graph over its own synthetic world, so one
run cannot influence the next through a shared mock tracker. Approval is
answered by the harness rather than a person: a scenario that expects a write
is resumed with a yes, which is how "the write ran, and only after approval"
becomes something the suite can assert rather than assume.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.types import Command

from app.adapters.mock.providers import (
    MockCodeProvider,
    MockIssueProvider,
    MockLogProvider,
    MockMonitoringProvider,
)
from app.agent.graph import build_graph, run_config
from app.agent.state import initial_state
from app.evaluation.metrics import RunScore, SuiteScore, score_run
from app.evaluation.scenarios import SUITE, EvalScenario

log = structlog.get_logger(__name__)


async def run_scenario(
    expected: EvalScenario,
    *,
    model: BaseChatModel | None = None,
    use_llm: bool = False,
) -> RunScore:
    """Execute one scenario end to end, including its approval if it pauses."""
    world = expected.scenario
    graph = build_graph(
        monitoring=MockMonitoringProvider(world),
        code=MockCodeProvider(world),
        logs=MockLogProvider(world),
        issues=MockIssueProvider(),
        model=model,
        use_llm=use_llm,
    )
    run_id = f"eval-{expected.name}-{uuid.uuid4().hex[:8]}"
    config = run_config(run_id)

    started = time.perf_counter()
    state = await graph.ainvoke(initial_state(run_id, expected.task), config)

    if state.get("__interrupt__"):
        # Тестовый стенд играет роль проверяющего. Ответ «да» позволяет проверить,
        # что запись выполнена и перед ней действительно требовалось решение.
        state = await graph.ainvoke(
            Command(resume={"approved": True, "decided_by": "evaluation-harness"}), config
        )
        state = {**state, "__interrupt__": ()}
        state["proposed_actions"] = state.get("proposed_actions") or []

    latency_ms = (time.perf_counter() - started) * 1000
    score = score_run(expected, state, latency_ms)
    log.info(
        "evaluation.scenario",
        scenario=expected.name,
        passed=score.passed,
        tool_calls=score.tool_calls,
        failures=[c.name for c in score.failures],
    )
    return score


async def run_suite(
    scenarios: Sequence[EvalScenario] = SUITE,
    *,
    model: BaseChatModel | None = None,
    use_llm: bool = False,
) -> SuiteScore:
    return SuiteScore(
        runs=tuple([await run_scenario(s, model=model, use_llm=use_llm) for s in scenarios])
    )


# ── Reporting ────────────────────────────────────────────────────────────────


def render_report(score: SuiteScore) -> str:
    """A report meant to be read in a terminal and pasted into a PR."""
    width = max((len(r.scenario) for r in score.runs), default=8) + 2
    lines = [
        f"{'scenario':<{width}} {'result':<7} {'tools':>5} {'steps':>5} {'waste':>5} {'ms':>7}",
        "─" * (width + 33),
    ]
    for run in score.runs:
        lines.append(
            f"{run.scenario:<{width}} {'PASS' if run.passed else 'FAIL':<7} "
            f"{run.tool_calls:>5} {run.steps:>5} {run.unnecessary_tool_calls:>5} "
            f"{run.latency_ms:>7.0f}"
        )
    lines += [
        "─" * (width + 33),
        f"{score.passed}/{score.total} passed  "
        f"mean {score.mean_tool_calls:.1f} tool calls  "
        f"mean {score.mean_latency_ms:.0f} ms  "
        f"{score.total_unnecessary_calls} wasted call(s)",
    ]

    failures = [(r, c) for r in score.runs for c in r.failures]
    if failures:
        lines += ["", "Failures:"]
        lines += [f"  {r.scenario} · {c.name}: {c.detail}" for r, c in failures]
    return "\n".join(lines)
