"""Контракт инструмента: схемы, границы рендеринга и сужение реестра."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.agent.tools.base import (
    MAX_DIGEST_CHARS,
    AgentTool,
    InvalidToolArgumentsError,
    InvalidToolResultError,
    ToolAccess,
    ToolRegistry,
    UnknownToolError,
)
from app.agent.tools.catalog import build_registry
from app.agent.tools.schemas import GetServiceMetricsArgs


class _Args(BaseModel):
    value: int


class _Result(BaseModel):
    text: str


def _tool(name: str, access: ToolAccess = ToolAccess.READ) -> AgentTool:
    async def handler(args: _Args) -> _Result:
        return _Result(text="x" * args.value)

    return AgentTool(
        name=name,
        description=name,
        args_schema=_Args,
        result_schema=_Result,
        access=access,
        handler=handler,
        render=lambda r: r.text,
    )


def test_registry_rejects_duplicate_names():
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolRegistry([_tool("a"), _tool("a")])


def test_unknown_tool_lists_what_is_available():
    registry = ToolRegistry([_tool("a"), _tool("b")])
    with pytest.raises(UnknownToolError, match="available: a, b"):
        registry.get("c")


def test_read_only_drops_write_tools():
    registry = ToolRegistry([_tool("read"), _tool("write", ToolAccess.WRITE)])
    assert registry.read_only().names == ("read",)
    assert len(registry.by_access(ToolAccess.WRITE)) == 1


def test_allowlisting_narrows_and_ignores_unknown_names():
    registry = ToolRegistry([_tool("a"), _tool("b")])
    assert registry.allowlisted(["b", "nonexistent"]).names == ("b",)


def test_arguments_are_validated_before_the_handler_runs():
    tool = _tool("a")
    with pytest.raises(InvalidToolArgumentsError, match="value"):
        tool.parse_arguments({"value": "not-an-int"})


def test_results_are_validated_too():
    tool = _tool("a")
    with pytest.raises(InvalidToolResultError):
        tool.parse_result({"wrong_field": 1})


def test_digest_is_truncated_so_one_tool_cannot_flood_the_prompt():
    tool = _tool("a")
    digest = tool.digest(_Result(text="y" * (MAX_DIGEST_CHARS * 3)))
    assert len(digest) == MAX_DIGEST_CHARS
    assert digest.endswith("…")


def test_json_schema_has_the_shape_tool_calling_apis_expect():
    tool = _tool("a")
    schema = tool.json_schema()
    assert schema["name"] == "a"
    assert schema["input_schema"]["properties"]["value"]["type"] == "integer"


def test_invented_metric_names_fail_validation():
    """Литерал метрики — защитное ограничение: выдуманный ряд не доходит до
    провайдера, поэтому агент получает исправимую ошибку схемы вместо
    непрозрачного сбоя поиска в бэкенде."""
    with pytest.raises(ValidationError):
        GetServiceMetricsArgs(service="billing-service", metric="cpu_temperature")


def test_catalogue_exposes_only_read_tools_in_v2(monitoring, code, logs):
    registry = build_registry(monitoring, code, logs)
    assert registry.by_access(ToolAccess.WRITE) == ()
    assert set(registry.names) == {
        "get_service_metrics",
        "get_recent_alerts",
        "get_recent_deployments",
        "get_commits",
        "get_pull_request",
        "get_error_groups",
    }


def test_every_tool_describes_itself_for_the_model(monitoring, code, logs):
    """Инструмент, которого модель не понимает, она будет применять неверно."""
    for tool in build_registry(monitoring, code, logs):
        assert len(tool.description) > 60, tool.name
        assert tool.json_schema()["input_schema"]["properties"], tool.name
