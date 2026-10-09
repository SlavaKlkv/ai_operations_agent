"""С какими MCP-серверами взаимодействует это развёртывание и на каких условиях.

Конфигурация серверов — это код, а не входные данные модели. Запуск не может
добавить сервер и не может расширить то, что разрешено существующему серверу, —
единственное направление, в котором эти настройки меняются во время выполнения,
это сужение.
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
    """Один MCP-сервер, к которому может подключиться этот агент."""

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
    """Сервер, запускаемый из этого репозитория, с использованием текущего интерпретатора.

    sys.executable, а не просто python, чтобы virtualenv, контейнер и
    оболочка разработчика разрешались в один и тот же интерпретатор.
    """
    return ServerSpec(
        name=kwargs.pop("name"),
        transport=Transport.STDIO,
        command=(sys.executable, "-m", module),
        **kwargs,
    )


def default_servers() -> tuple[ServerSpec, ...]:
    """Четыре сервера, поставляемые вместе с агентом.

    Это отдельные процессы, потому что они замещают четыре отдельные системы.
    Объединение их упростило бы деплой и сделало бы архитектуру ложью: в проде
    мониторинг и трекер задач — не один и тот же вендор, они не отказывают
    вместе и не заслуживают одинаковых прав.
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
