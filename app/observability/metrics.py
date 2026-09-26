"""Prometheus metrics for the agent.

What is measured here is chosen from the questions an operator actually asks
about an agent, which are not the questions they ask about a web service.
Request rate and p99 matter, but so do: how many tool calls an investigation
costs, how often the model is even reachable, how often a run ends in a
conclusion versus a shrug, and — the one that would wake someone up — whether
any write ever executed without an approval behind it.

Counters are deliberately low-cardinality. ``service`` is a label because
there are a handful of them; ``run_id`` is not, because there is one per run
and a label like that turns a metrics store into a log store.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

#: Отдельный реестр вместо глобального: приложение владеет
#: публикуемыми метриками, и случайный импорт не должен добавлять новый ряд.
REGISTRY = CollectorRegistry()

#: Корзины соответствуют реальной длительности расследования. С тестовыми или MCP-
#: С тестовыми провайдерами запуск занимает миллисекунды, с реальными — секунды,
#: а превышение минуты означает зависание.
LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

runs_started = Counter(
    "agent_runs_started_total",
    "Investigations started.",
    labelnames=("service",),
    registry=REGISTRY,
)

runs_finished = Counter(
    "agent_runs_finished_total",
    "Investigations that reached a terminal state, by how they ended.",
    labelnames=("service", "status"),
    registry=REGISTRY,
)

run_duration = Histogram(
    "agent_run_duration_seconds",
    "Wall-clock time of one investigation, excluding time spent awaiting approval.",
    labelnames=("service",),
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

run_tool_calls = Histogram(
    "agent_run_tool_calls",
    "Tool calls spent per investigation.",
    buckets=(1, 2, 3, 5, 8, 10, 12, 16, 24),
    registry=REGISTRY,
)

run_confidence = Histogram(
    "agent_run_confidence",
    "Confidence of the leading hypothesis, where one was produced.",
    buckets=(0.1, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    registry=REGISTRY,
)

tool_calls = Counter(
    "agent_tool_calls_total",
    "Tool calls, by tool and outcome.",
    labelnames=("tool", "outcome"),
    registry=REGISTRY,
)

tool_duration = Histogram(
    "agent_tool_duration_seconds",
    "Time one tool call took, including retries.",
    labelnames=("tool",),
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0),
    registry=REGISTRY,
)

llm_calls = Counter(
    "agent_llm_calls_total",
    "Model invocations, by outcome. A rising failure rate means the agent is "
    "silently running on its deterministic path.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

llm_tokens = Counter(
    "agent_llm_tokens_total",
    "Tokens consumed, by direction.",
    labelnames=("direction",),
    registry=REGISTRY,
)

approvals = Counter(
    "agent_approvals_total",
    "Human decisions on proposed writes.",
    labelnames=("decision",),
    registry=REGISTRY,
)

#: Критически важный алерт. Он всегда должен оставаться на нуле; любой рост
#: означает обход подтверждения — событие безопасности, а не обычную
#: проблему производительности.
unapproved_writes = Counter(
    "agent_unapproved_writes_total",
    "Write tools that executed without an approved decision. Must stay at zero.",
    labelnames=("tool",),
    registry=REGISTRY,
)

mcp_server_up = Gauge(
    "agent_mcp_server_up",
    "1 when an MCP server is connected, 0 when it is not.",
    labelnames=("server", "required"),
    registry=REGISTRY,
)

checkpointer_durable = Gauge(
    "agent_checkpointer_durable",
    "1 when a paused approval would survive a restart.",
    registry=REGISTRY,
)
