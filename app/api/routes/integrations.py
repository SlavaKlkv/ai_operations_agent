"""Обзор слоя интеграций.

Агенту, чьи данные приходят из четырёх внешних систем, нужен ответ на вопрос
«какая из них на самом деле работает и что она мне предлагает», не требующий
запуска расследования. Этот эндпоинт и есть такой ответ, и он же показывает
читателю репозитория, что MCP действительно используется: список инструментов
здесь обнаруживается в рантайме, а не объявляется в коде.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from app.mcp.client import MCPToolPool, RemoteTool, ServerStatus
from app.mcp.runtime import get_pool

router = APIRouter(prefix="/mcp", tags=["integrations"])


class ToolView(BaseModel):
    name: str
    server: str
    access: str
    description: str


class ServerView(BaseModel):
    name: str
    connected: bool
    required: bool
    tool_count: int
    error: str | None = None


class IntegrationsView(BaseModel):
    healthy: bool
    servers: list[ServerView] = Field(default_factory=list)
    tools: list[ToolView] = Field(default_factory=list)


def _server_view(server: ServerStatus) -> ServerView:
    return ServerView(
        name=server.name,
        connected=server.connected,
        required=server.required,
        tool_count=server.tool_count,
        error=server.error,
    )


def _tool_view(tool: RemoteTool) -> ToolView:
    return ToolView(
        name=tool.name,
        server=tool.server,
        access="read" if tool.read_only else "write",
        description=tool.description,
    )


@router.get(
    "/servers",
    response_model=IntegrationsView,
    summary="Connected MCP servers and the tools they advertise",
)
async def list_servers(
    response: Response, pool: MCPToolPool = Depends(get_pool)
) -> IntegrationsView:
    """Сообщить состояние слоя интеграций и отразить это в статус-коде.

    Деградировавший слой интеграций возвращает 503, а не бодрый 200 с
    healthy: false, запрятанным в теле — этот эндпоинт задуман как
    проба готовности, а проба, которая всегда успешна, таковой не является.
    """
    await pool.connect()
    if not pool.healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return IntegrationsView(
        healthy=pool.healthy,
        servers=[_server_view(s) for s in pool.status],
        tools=sorted((_tool_view(t) for t in pool.tools()), key=lambda t: (t.server, t.name)),
    )
