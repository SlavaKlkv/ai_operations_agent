"""Пул клиентов: обнаружение, классификация, деградация и форма отказов.

Эти тесты о том, что происходит с агентом, когда внешняя система ведёт себя
неправильно, поэтому в них используются серверы, намеренно ведущие себя плохо:
один отказывается запускаться, другой никогда не отвечает, третий забывает
проставить себе аннотации.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from app.mcp.client import (
    MCPError,
    MCPToolPool,
    ServerUnavailable,
    ToolCallFailed,
    UnknownTool,
    WriteNotPermitted,
)
from app.mcp.config import ServerSpec, Transport
from app.mcp_servers.code import build_server as build_code_server
from app.mcp_servers.incident import IssueStore
from app.mcp_servers.incident import build_server as build_incident_server
from app.mcp_servers.knowledge import build_server as build_knowledge_server
from app.mcp_servers.monitoring import build_server as build_monitoring_server

WINDOW = {"start": "2026-03-17T14:00:00Z", "end": "2026-03-17T15:00:00Z"}


class Out(BaseModel):
    """Тип возврата для одноразовых серверов ниже.

    Объявлен на уровне модуля, потому что from __future__ import annotations
    превращает аннотации возврата в строки, а MCP разрешает их в пространстве имён
    модуля, когда выводит схему вывода.
    """

    ok: bool = True


def _spec(name: str, factory, **kwargs) -> ServerSpec:
    return ServerSpec(name=name, transport=Transport.IN_PROCESS, factory=factory, **kwargs)


@pytest.fixture
def specs(scenario):
    return (
        _spec("monitoring", lambda: build_monitoring_server(scenario)),
        _spec("code", lambda: build_code_server(scenario)),
        _spec("incident", lambda: build_incident_server(IssueStore())),
        _spec("knowledge", build_knowledge_server, required=False),
    )


@pytest.fixture
async def pool(specs):
    async with MCPToolPool(specs) as connected:
        yield connected


# ── Обнаружение ──────────────────────────────────────────────────────────────


async def test_every_configured_server_is_connected_and_reported(pool):
    assert pool.healthy
    assert {s.name for s in pool.status if s.connected} == {
        "monitoring",
        "code",
        "incident",
        "knowledge",
    }
    assert sum(s.tool_count for s in pool.status) == 13


async def test_read_and_write_are_split_by_the_servers_own_annotations(pool):
    """Ничто здесь не знает, что create_issue опасен по своему имени."""
    assert {t.name for t in pool.tools(read_only=False)} == {
        "create_issue",
        "add_issue_comment",
    }
    assert "get_service_metrics" in {t.name for t in pool.tools(read_only=True)}


async def test_an_unannotated_tool_is_assumed_to_write():
    """Безопасное поведение по умолчанию не должно зависеть от того, вспомнит ли
    сервер объявить себя — новый сервер не получает доверия, о котором не просил."""

    silent = MCPServer(name="silent", version="1.0.0")

    @silent.tool(description="A tool that declares nothing about itself at all.")
    async def mystery() -> Out:
        return Out()

    async with MCPToolPool((_spec("silent", lambda: silent),)) as connected:
        assert connected.get("mystery").read_only is False
        with pytest.raises(WriteNotPermitted):
            await connected.call("mystery", {})


async def test_the_allowlist_hides_tools_the_deployment_did_not_accept(scenario):
    """Сервер, у которого появился новый инструмент, не должен молча расширять доступ."""
    narrowed = _spec(
        "monitoring",
        lambda: build_monitoring_server(scenario),
        allowed_tools=frozenset({"get_error_rate"}),
    )
    async with MCPToolPool((narrowed,)) as connected:
        assert [t.name for t in connected.tools()] == ["get_error_rate"]
        with pytest.raises(UnknownTool, match="get_service_metrics"):
            connected.get("get_service_metrics")


async def test_two_servers_offering_the_same_tool_name_is_a_configuration_error(scenario):
    duplicated = (
        _spec("a", lambda: build_monitoring_server(scenario)),
        _spec("b", lambda: build_monitoring_server(scenario)),
    )
    with pytest.raises(MCPError, match="offered by both"):
        async with MCPToolPool(duplicated):
            pass


async def test_unknown_tools_name_what_is_available(pool):
    with pytest.raises(UnknownTool, match="available: add_issue_comment"):
        pool.get("drop_database")


async def test_resources_can_be_read_through_the_pool(pool):
    assert "billing-service" in await pool.read_resource("monitoring", "monitoring://services")


# ── Вызовы ───────────────────────────────────────────────────────────────────


async def test_a_successful_call_returns_structured_content(pool):
    result = await pool.call("get_error_rate", {"service": "billing-service", **WINDOW})
    assert 0.0 < result["result"] < 1.0


async def test_a_write_needs_approval_and_then_goes_through(pool):
    with pytest.raises(WriteNotPermitted, match="no approval was given"):
        await pool.call("create_issue", {"title": "Unapproved", "body": "x"})

    created = await pool.call("create_issue", {"title": "Approved", "body": "x"}, approved=True)
    assert created["key"].startswith("OPS-")


async def test_approval_is_per_call_not_a_mode_the_pool_stays_in(pool):
    await pool.call("create_issue", {"title": "First", "body": "x"}, approved=True)
    with pytest.raises(WriteNotPermitted):
        await pool.call("create_issue", {"title": "Second", "body": "x"})


async def test_a_tool_failure_is_distinguishable_from_an_outage(pool):
    """Агент по-разному реагирует на «этого не существует» и «сервер
    пропал», поэтому эти два случая не должны приходить как одно исключение."""
    with pytest.raises(ToolCallFailed, match="no error_rate series"):
        await pool.call("get_error_rate", {"service": "ghost-service", **WINDOW})


# ── Деградация ───────────────────────────────────────────────────────────────


def _broken_server() -> MCPServer:
    raise RuntimeError("this server will not start")


async def test_an_optional_server_that_will_not_start_leaves_the_pool_healthy(scenario):
    specs = (
        _spec("monitoring", lambda: build_monitoring_server(scenario)),
        _spec("knowledge", _broken_server, required=False),
    )
    async with MCPToolPool(specs) as connected:
        assert connected.healthy, "an optional server is not load-bearing"
        broken = next(s for s in connected.status if s.name == "knowledge")
        assert broken.connected is False
        assert "will not start" in broken.error
        # Остальная часть слоя интеграции не затронута.
        assert await connected.call("get_error_rate", {"service": "billing-service", **WINDOW})


async def test_a_required_server_that_will_not_start_marks_the_pool_unhealthy(scenario):
    specs = (
        _spec("monitoring", _broken_server),
        _spec("knowledge", build_knowledge_server, required=False),
    )
    async with MCPToolPool(specs) as connected:
        assert connected.healthy is False
        with pytest.raises(UnknownTool):
            await connected.call("get_error_rate", {"service": "billing-service", **WINDOW})


async def test_a_failing_server_does_not_raise_out_of_connect(scenario):
    """Отказы при запуске фиксируются, а не пробрасываются: одна мёртвая
    интеграция не должна мешать процессу подняться."""
    async with MCPToolPool((_spec("broken", _broken_server),)) as connected:
        assert [s.connected for s in connected.status] == [False]


async def test_a_slow_server_is_cut_off_by_its_timeout():
    slow = MCPServer(name="slow", version="1.0.0")

    @slow.tool(
        description="A tool that takes far longer than the configured budget allows.",
        annotations=ToolAnnotations(read_only_hint=True),
    )
    async def crawl() -> Out:
        await asyncio.sleep(5)
        return Out()

    spec = _spec("slow", lambda: slow, timeout_seconds=0.05)
    async with MCPToolPool((spec,)) as connected:
        with pytest.raises(ServerUnavailable, match="timed out"):
            await connected.call("crawl", {})


async def test_closing_the_pool_releases_every_connection(specs):
    connected = MCPToolPool(specs)
    await connected.connect()
    assert connected.tools()
    await connected.aclose()
    assert connected.tools() == ()


# ── HTTP-интерфейс ───────────────────────────────────────────────────────────


async def test_the_integrations_endpoint_reports_discovered_tools(specs):
    """Список инструментов обнаруживается по протоколу во время выполнения, а не объявляется."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from app.mcp.runtime import get_pool

    connected = MCPToolPool(specs)
    app = create_app()
    app.dependency_overrides[get_pool] = lambda: connected

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/mcp/servers")
    await connected.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["healthy"] is True
    assert {s["name"] for s in body["servers"]} == {"monitoring", "code", "incident", "knowledge"}
    by_name = {t["name"]: t for t in body["tools"]}
    assert by_name["get_service_metrics"]["access"] == "read"
    assert by_name["create_issue"]["access"] == "write"


async def test_a_degraded_integration_layer_answers_503(scenario):
    """Задумана как проба готовности, поэтому должна уметь сообщать об отказе."""
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from app.mcp.runtime import get_pool

    connected = MCPToolPool((_spec("monitoring", _broken_server),))
    app = create_app()
    app.dependency_overrides[get_pool] = lambda: connected

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/mcp/servers")
    await connected.aclose()

    assert response.status_code == 503
    assert response.json()["healthy"] is False
