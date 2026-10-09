"""Серверы MCP, проверяемые через настоящий клиент по протоколу.

Каждый тест здесь подключает реальный mcp.Client к объекту сервера.
Транспорт внутрипроцессный, чтобы набор оставался быстрым, но обнаружение,
JSON-RPC, валидация схем и отображение ошибок — настоящие: проверка функций-
обработчиков напрямую пропустила бы именно тот слой, для которого и существуют
эти файлы.
"""

from __future__ import annotations

import pytest
from mcp import Client

from app.mcp_servers.code import build_server as build_code_server
from app.mcp_servers.incident import IssueStore, seed_issues
from app.mcp_servers.incident import build_server as build_incident_server
from app.mcp_servers.knowledge import build_server as build_knowledge_server
from app.mcp_servers.monitoring import build_server as build_monitoring_server

WINDOW = {"start": "2026-03-17T14:00:00Z", "end": "2026-03-17T15:00:00Z"}


def _payload(result):
    """Разворачивает обёртку {"result": ...}, которой MCP окружает скаляры/списки."""
    content = result.structured_content
    return content["result"] if set(content) == {"result"} else content


def _error(result) -> str:
    return " ".join(block.text for block in result.content)


# ── Мониторинг ───────────────────────────────────────────────────────────────


async def test_monitoring_advertises_read_only_tools_and_a_resource(scenario):
    async with Client(build_monitoring_server(scenario)) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {
            "get_service_metrics",
            "get_error_rate",
            "get_recent_alerts",
            "get_error_groups",
        }
        assert all(t.annotations.read_only_hint for t in tools), (
            "an unannotated tool is treated as a write by the client, so a "
            "read server that forgets to declare itself becomes unusable"
        )
        assert [str(r.uri) for r in (await client.list_resources()).resources] == [
            "monitoring://services"
        ]


async def test_metrics_come_back_with_a_precomputed_summary(scenario):
    async with Client(build_monitoring_server(scenario)) as client:
        series = _payload(
            await client.call_tool(
                "get_service_metrics",
                {"service": "billing-service", "metric": "error_rate", **WINDOW},
            )
        )
    assert series["sample_count"] == 61
    assert series["peak_value"] > series["first_value"] * 10
    assert series["peak_at"].startswith("2026-03-17T14:3")


async def test_errors_are_aggregated_never_returned_raw(scenario):
    """Граница протокола — это то место, где нужно останавливать объём логов."""
    async with Client(build_monitoring_server(scenario)) as client:
        groups = _payload(
            await client.call_tool("get_error_groups", {"service": "billing-service", **WINDOW})
        )
    assert {g["error_type"] for g in groups} == {"TypeError", "GatewayTimeout"}
    assert groups[0]["count"] > 50
    assert len(groups) < 10, "aggregation, not a log stream"


async def test_min_count_drops_the_long_tail(scenario):
    async with Client(build_monitoring_server(scenario)) as client:
        groups = _payload(
            await client.call_tool(
                "get_error_groups", {"service": "billing-service", "min_count": 50, **WINDOW}
            )
        )
    assert [g["error_type"] for g in groups] == ["TypeError"]


async def test_an_anticipated_failure_keeps_its_message(scenario):
    """Падение сообщает вызывающему только имя инструмента; ToolFailure объясняет."""
    async with Client(build_monitoring_server(scenario)) as client:
        result = await client.call_tool("get_error_rate", {"service": "does-not-exist", **WINDOW})
    assert result.is_error
    assert "no error_rate series" in _error(result)


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ({"start": "yesterday", "end": "2026-03-17T15:00:00Z"}, "ISO 8601"),
        ({"start": "2026-03-17T15:00:00Z", "end": "2026-03-17T14:00:00Z"}, "not be earlier"),
    ],
)
async def test_the_server_validates_its_own_inputs(scenario, window, expected):
    """Сервер не может рассчитывать на благонадёжного клиента: вызвать его может
    что угодно, говорящее по протоколу, включая что-то управляемое языковой моделью."""
    async with Client(build_monitoring_server(scenario)) as client:
        result = await client.call_tool(
            "get_service_metrics",
            {"service": "billing-service", "metric": "error_rate", **window},
        )
    assert result.is_error
    assert expected in _error(result)


async def test_the_services_resource_describes_what_is_monitored(scenario):
    async with Client(build_monitoring_server(scenario)) as client:
        text = (await client.read_resource("monitoring://services")).contents[0].text
    assert "billing-service" in text
    assert "error_rate" in text


# ── Код ──────────────────────────────────────────────────────────────────────


async def test_deployments_come_back_newest_first(scenario):
    async with Client(build_code_server(scenario)) as client:
        found = _payload(
            await client.call_tool(
                "get_recent_deployments",
                {
                    "service": "billing-service",
                    "start": "2026-03-17T00:00:00Z",
                    "end": "2026-03-17T15:00:00Z",
                },
            )
        )
    assert [d["version"] for d in found] == ["v1.8.4", "v1.8.3"]


async def test_a_missing_pull_request_is_an_error_not_an_empty_result(scenario):
    async with Client(build_code_server(scenario)) as client:
        result = await client.call_tool("get_pull_request", {"service": "x", "number": 99999})
    assert result.is_error
    assert "no pull request" in _error(result)


async def test_commits_carry_the_files_they_changed(scenario):
    async with Client(build_code_server(scenario)) as client:
        commits = _payload(
            await client.call_tool(
                "get_commits",
                {
                    "service": "billing-service",
                    "start": "2026-03-17T00:00:00Z",
                    "end": "2026-03-17T15:00:00Z",
                },
            )
        )
    release = next(c for c in commits if c["sha"].startswith("9f2c41ab"))
    assert {f["path"] for f in release["files"]} == {"billing/charge.py", "billing/tax/rates.py"}


# ── Инцидент ─────────────────────────────────────────────────────────────────


async def test_write_tools_declare_themselves_as_writes():
    """Клиент классифицирует по аннотации, поэтому это объявление — вся основа,
    по которой create_issue считается опасным."""
    async with Client(build_incident_server(IssueStore(seed_issues()))) as client:
        by_name = {t.name: t for t in (await client.list_tools()).tools}
    assert by_name["search_issues"].annotations.read_only_hint is True
    assert by_name["create_issue"].annotations.read_only_hint is False
    assert by_name["add_issue_comment"].annotations.read_only_hint is False


async def test_searching_finds_the_seeded_issue():
    async with Client(build_incident_server(IssueStore(seed_issues()))) as client:
        found = _payload(
            await client.call_tool(
                "search_issues", {"query": "timeout", "service": "billing-service"}
            )
        )
    assert [i["key"] for i in found] == ["OPS-1"]


async def test_creating_an_issue_writes_through_to_the_store():
    store = IssueStore(seed_issues())
    async with Client(build_incident_server(store)) as client:
        created = _payload(
            await client.call_tool(
                "create_issue",
                {"title": "5xx after v1.8.4", "body": "Details.", "service": "billing-service"},
            )
        )
    assert created["key"] == "OPS-2"
    assert store.created_count == 1
    assert store.get("OPS-2").title == "5xx after v1.8.4"


async def test_a_duplicate_title_is_refused_by_the_server():
    """Забота на стороне клиента не заменяет: сервер тоже должен отказывать."""
    store = IssueStore(seed_issues())
    async with Client(build_incident_server(store)) as client:
        await client.call_tool("create_issue", {"title": "Same", "body": "a"})
        result = await client.call_tool("create_issue", {"title": "  same  ", "body": "b"})
    assert result.is_error
    assert "already exists" in _error(result)
    assert store.created_count == 1


async def test_the_creation_cap_stops_a_runaway_agent(monkeypatch):
    import app.mcp_servers.incident as incident

    monkeypatch.setattr(incident, "MAX_ISSUES_PER_PROCESS", 2)
    store = incident.IssueStore()
    async with Client(build_incident_server(store)) as client:
        for i in range(2):
            assert not (
                await client.call_tool("create_issue", {"title": f"Issue {i}", "body": "x"})
            ).is_error
        blocked = await client.call_tool("create_issue", {"title": "Issue 3", "body": "x"})
    assert blocked.is_error
    assert "limit" in _error(blocked)
    assert store.created_count == 2


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"title": "   ", "body": "x"}, "must not be empty"),
        ({"title": "t" * 500, "body": "x"}, "at most"),
    ],
)
async def test_the_server_bounds_what_it_will_store(arguments, expected):
    async with Client(build_incident_server(IssueStore())) as client:
        result = await client.call_tool("create_issue", arguments)
    assert result.is_error
    assert expected in _error(result)


async def test_the_policy_resource_states_the_write_rules():
    async with Client(build_incident_server(IssueStore())) as client:
        text = (await client.read_resource("incident://policy")).contents[0].text
    assert "requires_human_approval" in text
    assert "create_issue" in text


# ── База знаний ──────────────────────────────────────────────────────────────


async def test_runbook_search_ranks_the_relevant_document_first():
    async with Client(build_knowledge_server()) as client:
        hits = _payload(
            await client.call_tool(
                "search_runbooks",
                {"query": "tax rate TypeError charge", "service": "billing-service"},
            )
        )
    assert hits[0]["doc_id"] == "rb-tax-engine"
    assert hits[0]["score"] > hits[-1]["score"]
    assert "apply_tax_rate" in hits[0]["excerpt"]


async def test_the_service_filter_biases_ranking_without_hiding_results():
    async with Client(build_knowledge_server()) as client:
        hits = _payload(
            await client.call_tool(
                "search_runbooks", {"query": "rollback", "service": "billing-service", "limit": 5}
            )
        )
    assert hits[0]["doc_id"] == "rb-billing-rollback"


async def test_a_query_with_no_match_returns_nothing_rather_than_guessing():
    async with Client(build_knowledge_server()) as client:
        hits = _payload(await client.call_tool("search_runbooks", {"query": "kubernetes etcd"}))
    assert hits == []


async def test_an_empty_query_is_refused():
    async with Client(build_knowledge_server()) as client:
        result = await client.call_tool("search_runbooks", {"query": "   "})
    assert result.is_error
    assert "at least one searchable word" in _error(result)


async def test_a_runbook_can_be_fetched_in_full_after_search():
    async with Client(build_knowledge_server()) as client:
        document = _payload(await client.call_tool("get_runbook", {"doc_id": "rb-oncall-triage"}))
        missing = await client.call_tool("get_runbook", {"doc_id": "rb-nope"})
    assert "Rollback first, root-cause after" in document["body"]
    assert missing.is_error
    assert "known ids" in _error(missing)
