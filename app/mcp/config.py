"""Which MCP servers this deployment talks to, and on what terms.

Server configuration is code, not model input. A run cannot add a server, and
cannot widen what an existing server is allowed to do — the only direction
these settings move at runtime is narrower.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — цикл импорта важен только для проверки типов
    from mcp.server.mcpserver import MCPServer


class Transport(StrEnum):
    #: Запустить сервер подпроцессом и общаться через stdin/stdout.
    STDIO = "stdio"
    #: Подключиться по HTTP к уже запущенному серверу.
    HTTP = "http"
    #: Запустить объект сервера в текущем процессе, сохранив протокол.
    #:
    #: Это не обход MCP: тот же клиент, JSON-RPC, обнаружение и аннотации
    #: инструментов; отличается только транспорт. Режим нужен,
    #: потому что четыре подпроцесса на тест настолько замедляют выполнение, что
    #: иначе тесты слоя интеграции стали бы слишком медленными, а непроверенный
    #: слой интеграции обязательно сломается.
    IN_PROCESS = "in_process"


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """One MCP server this agent may connect to."""

    name: str
    transport: Transport
    #: Для stdio: команда запуска и аргументы.
    command: tuple[str, ...] = ()
    #: Для HTTP: URL эндпоинта.
    url: str | None = None
    #: Для IN_PROCESS: фабрика объекта сервера для подключения.
    factory: Callable[[], MCPServer] | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: Инструменты сервера, разрешённые в этом развёртывании. Пустое значение
    #: означает все предлагаемые инструменты; непустое множество — allowlist,
    #: применяемый при обнаружении, чтобы новый инструмент не получил доступ незаметно.
    allowed_tools: frozenset[str] = frozenset()
    #: Время ожидания одного вызова до отказа от сервера, в секундах.
    timeout_seconds: float = 15.0
    #: Может ли запуск продолжиться без этого сервера. Сервер мониторинга критичен,
    #: а сервер знаний — нет.
    required: bool = True

    def permits(self, tool_name: str) -> bool:
        return not self.allowed_tools or tool_name in self.allowed_tools


def _stdio(module: str, **kwargs) -> ServerSpec:
    """A server launched from this repository, using the running interpreter.

    ``sys.executable`` rather than a bare ``python`` so a virtualenv, a
    container and a developer's shell all resolve to the same interpreter.
    """
    return ServerSpec(
        name=kwargs.pop("name"),
        transport=Transport.STDIO,
        command=(sys.executable, "-m", module),
        **kwargs,
    )


def default_servers() -> tuple[ServerSpec, ...]:
    """The four servers the agent ships with.

    They are separate processes because they stand in for four separate
    systems. Merging them would make the deployment simpler and the
    architecture a lie: in production, monitoring and the issue tracker are
    not the same vendor, do not fail together, and do not deserve the same
    permissions.
    """
    return (
        _stdio(
            "app.mcp_servers.monitoring",
            name="monitoring",
            allowed_tools=frozenset(
                {"get_service_metrics", "get_error_rate", "get_recent_alerts", "get_error_groups"}
            ),
        ),
        _stdio(
            "app.mcp_servers.code",
            name="code",
            allowed_tools=frozenset({"get_recent_deployments", "get_commits", "get_pull_request"}),
        ),
        _stdio(
            "app.mcp_servers.incident",
            name="incident",
            allowed_tools=frozenset(
                {"search_issues", "get_issue", "create_issue", "add_issue_comment"}
            ),
        ),
        _stdio(
            "app.mcp_servers.knowledge",
            name="knowledge",
            allowed_tools=frozenset({"search_runbooks", "get_runbook"}),
            # Расследование без ранбука хуже, но всё же возможно.
            required=False,
        ),
    )
