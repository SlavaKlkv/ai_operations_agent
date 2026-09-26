"""What a correct investigation looks like, stated before it runs.

An evaluation suite is only worth having if the expectations are specific
enough to fail. "Finds the cause" is not; "names v1.8.4, reaches at least 0.8
confidence, never calls get_pull_request, and proposes exactly one write" is.

The three scenarios are chosen to disagree with each other. One has a release
that really did cause the incident; one has an incident with no release
anywhere near it; one has nothing wrong at all. An agent that pattern-matches
"errors, therefore blame the last deploy" passes the first and fails the other
two, which is the whole reason the other two exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.adapters.mock.dataset import BILLING_5XX, CHECKOUT_DEPENDENCY, SEARCH_HEALTHY, Scenario


@dataclass(frozen=True, slots=True)
class EvalScenario:
    """One investigation and the answer it is supposed to reach."""

    name: str
    scenario: Scenario
    task: str
    description: str

    # ── Какой вывод ожидается ────────────────────────────────────────────────
    expected_service: str
    #: Подстроки, которые должны присутствовать в основной гипотезе. Используются
    #: подстроки, а не точное предложение, чтобы формулировка могла меняться,
    #: а неверное утверждение — нет.
    expected_in_conclusion: tuple[str, ...] = ()
    #: Подстроки, которых быть не должно: здесь правдоподобная, но неверная
    #: правдоподобный неверный ответ был обнаружен.
    forbidden_in_conclusion: tuple[str, ...] = ()
    min_confidence: float = 0.0
    max_confidence: float = 1.0

    # ── Как агент должен прийти к выводу ─────────────────────────────────────
    required_tools: frozenset[str] = frozenset()
    forbidden_tools: frozenset[str] = frozenset()
    #: При превышении запуск тратит лишние вызовы.
    max_tool_calls: int = 12
    max_steps: int = 30

    # ── Что агент должен сделать ─────────────────────────────────────────────
    expects_proposal: bool = False
    #: Каждое доказательство должно называть реально вызванный инструмент.
    requires_grounded_evidence: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)


BASELINE_TOOLS = frozenset(
    {"get_service_metrics", "get_recent_deployments", "get_error_groups", "get_recent_alerts"}
)


SUITE: tuple[EvalScenario, ...] = (
    EvalScenario(
        name="release-caused-incident",
        scenario=BILLING_5XX,
        task=(
            "После последнего релиза billing-service резко выросло количество 5xx. "
            "Разберись, что произошло, и подготовь issue."
        ),
        description=(
            "A release five minutes before the spike changed the file in the failing "
            "stack frame. The agent should find it, and should not be distracted by "
            "an unrelated service deployed two minutes *into* the incident."
        ),
        expected_service="billing-service",
        expected_in_conclusion=("v1.8.4", "9f2c41ab"),
        forbidden_in_conclusion=("search-service", "v3.1.0"),
        min_confidence=0.8,
        required_tools=BASELINE_TOOLS | {"get_commits"},
        max_tool_calls=10,
        expects_proposal=True,
        tags=("deployment", "happy-path"),
    ),
    EvalScenario(
        name="dependency-degradation",
        scenario=CHECKOUT_DEPENDENCY,
        task="checkout-service отдаёт 5xx последние полчаса. Разберись, что происходит.",
        description=(
            "Errors and tail latency rise together while request rate stays flat, and "
            "the only deployment is eight hours old. The correct answer says no release "
            "explains this; blaming the old deploy would be the characteristic failure."
        ),
        expected_service="checkout-service",
        expected_in_conclusion=("not explained by any deployment",),
        forbidden_in_conclusion=("v4.2.0", "b1d0f7c9"),
        max_confidence=0.5,
        required_tools=BASELINE_TOOLS,
        max_tool_calls=8,
        expects_proposal=False,
        tags=("dependency", "negative"),
    ),
    EvalScenario(
        name="no-incident",
        scenario=SEARCH_HEALTHY,
        task="жалуются на search-service, посмотри что там происходит",
        description=(
            "Nothing is wrong. A report of a problem does not make one exist, and the "
            "right answer is that nothing was found — not the most plausible story "
            "that could be told about a recent deploy."
        ),
        expected_service="search-service",
        expected_in_conclusion=("No conclusive cause",),
        forbidden_in_conclusion=("v3.1.0", "fuzzy matching"),
        max_confidence=0.3,
        max_tool_calls=8,
        expects_proposal=False,
        tags=("negative", "false-alarm"),
    ),
)


def by_name(name: str) -> EvalScenario:
    for scenario in SUITE:
        if scenario.name == name:
            return scenario
    known = ", ".join(s.name for s in SUITE)
    raise KeyError(f"no evaluation scenario {name!r}; known: {known}")
