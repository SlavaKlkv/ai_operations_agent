"""Утверждение, которое должен заслужить V3: агент не зависит от того, где живут его данные.

Если MCP — это действительно слой интеграции, а не вторая система инструментов,
то один и тот же граф, один и тот же реестр, одни и те же защитные ограничения
и один и тот же анализ должны получаться одинаковыми независимо от того,
являются ли провайдеры внутрипроцессными моками или четырьмя серверами,
доступными по протоколу. Эти тесты сравнивают оба варианта напрямую.
"""

from __future__ import annotations

import pytest

from app.adapters.mcp_backed import (
    MCPCodeProvider,
    MCPKnowledgeProvider,
    MCPLogProvider,
    MCPMonitoringProvider,
    RemoteDataError,
)
from app.agent.graph import build_graph, run_config
from app.agent.state import initial_state
from app.mcp.client import MCPToolPool
from app.mcp.config import ServerSpec, Transport
from app.mcp_servers.code import build_server as build_code_server
from app.mcp_servers.incident import IssueStore
from app.mcp_servers.incident import build_server as build_incident_server
from app.mcp_servers.knowledge import build_server as build_knowledge_server
from app.mcp_servers.monitoring import build_server as build_monitoring_server


def _spec(name: str, factory, **kwargs) -> ServerSpec:
    return ServerSpec(name=name, transport=Transport.IN_PROCESS, factory=factory, **kwargs)


@pytest.fixture
async def pool(scenario):
    specs = (
        _spec("monitoring", lambda: build_monitoring_server(scenario)),
        _spec("code", lambda: build_code_server(scenario)),
        _spec("incident", lambda: build_incident_server(IssueStore())),
        _spec("knowledge", build_knowledge_server, required=False),
    )
    async with MCPToolPool(specs) as connected:
        yield connected


@pytest.fixture
def over_mcp(pool):
    return build_graph(
        monitoring=MCPMonitoringProvider(pool),
        code=MCPCodeProvider(pool),
        logs=MCPLogProvider(pool),
        use_llm=False,
    )


async def test_the_investigation_reaches_the_same_conclusion_over_mcp(
    over_mcp, monitoring, code, logs, fresh_state
):
    remote = await over_mcp.ainvoke(fresh_state, run_config(fresh_state["run_id"]))
    local = await build_graph(monitoring=monitoring, code=code, logs=logs, use_llm=False).ainvoke(
        initial_state("local", fresh_state["task"]), run_config("local")
    )

    assert remote["analysis"].summary == local["analysis"].summary
    assert remote["analysis"].confidence == local["analysis"].confidence
    assert [c.tool for c in remote["tool_calls"]] == [c.tool for c in local["tool_calls"]]


async def test_the_agents_tool_names_are_its_own_not_the_servers(over_mcp, fresh_state):
    """Реестр не порождается из того, что рекламируют серверы, поэтому сервер
    не может расширить доступ агента, отредактировав свой манифест."""
    final = await over_mcp.ainvoke(fresh_state, run_config(fresh_state["run_id"]))
    assert {c.tool for c in final["tool_calls"]} <= {
        "get_service_metrics",
        "get_recent_deployments",
        "get_error_groups",
        "get_recent_alerts",
        "get_commits",
        "get_pull_request",
    }
    assert "create_issue" not in {c.tool for c in final["tool_calls"]}


async def test_remote_data_is_revalidated_into_domain_models(pool):
    """Ответ сервера — это недоверенный ввод, поэтому он разбирается на границе."""
    provider = MCPMonitoringProvider(pool)
    series = await provider.get_service_metrics(
        "billing-service",
        "error_rate",
        initial_window()[0],
        initial_window()[1],
    )
    assert series.unit == "ratio"
    assert series.peak().value > series.points[0].value
    assert all(p.timestamp.tzinfo is not None for p in series.points)


async def test_a_malformed_response_fails_at_the_boundary(pool, monkeypatch):
    """А не на три слоя позже, где причину было бы уже не восстановить."""
    provider = MCPMonitoringProvider(pool)

    async def nonsense(name, arguments, *, approved=False):
        return {"service": "billing-service", "metric": "error_rate", "points": "not a list"}

    monkeypatch.setattr(pool, "call", nonsense)
    with pytest.raises(RemoteDataError, match="malformed metrics"):
        await provider.get_service_metrics("billing-service", "error_rate", *initial_window())


async def test_a_missing_pull_request_is_none_not_an_exception(pool):
    """На уровне протокола это ошибка; на уровне предметной области — ответ,
    и агент не должен отличать его от сбоя."""
    provider = MCPCodeProvider(pool)
    assert await provider.get_pull_request("billing-service", 99999) is None
    found = await provider.get_pull_request("billing-service", 482)
    assert found is not None and found.number == 482


async def test_the_knowledge_server_is_reachable_as_a_tool(pool):
    hits = await MCPKnowledgeProvider(pool).search_runbooks(
        "rollback billing", service="billing-service"
    )
    assert hits and hits[0].doc_id == "rb-billing-rollback"


async def test_losing_the_optional_server_does_not_change_the_conclusion(
    scenario, fresh_state, over_mcp
):
    """База знаний объявлена необязательной; расследование должно выжить без неё."""
    baseline = await over_mcp.ainvoke(fresh_state, run_config(fresh_state["run_id"]))

    def broken() -> object:
        raise RuntimeError("knowledge server is down")

    degraded_specs = (
        _spec("monitoring", lambda: build_monitoring_server(scenario)),
        _spec("code", lambda: build_code_server(scenario)),
        _spec("knowledge", broken, required=False),
    )
    async with MCPToolPool(degraded_specs) as degraded:
        assert degraded.healthy
        graph = build_graph(
            monitoring=MCPMonitoringProvider(degraded),
            code=MCPCodeProvider(degraded),
            logs=MCPLogProvider(degraded),
            use_llm=False,
        )
        final = await graph.ainvoke(
            initial_state("degraded", fresh_state["task"]), run_config("degraded")
        )

    assert final["analysis"].summary == baseline["analysis"].summary


def initial_window():
    from app.adapters.mock.dataset import DAY

    return DAY.replace(hour=14, minute=0), DAY.replace(hour=15, minute=0)
