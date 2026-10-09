"""Общие вспомогательные функции для MCP-серверов в этом репозитории.

Намеренно небольшие. Эти серверы замещают четыре разные внешние системы, и
совместное использование чего-либо сверх работы с временными метками и формы
ошибок тихо связало бы системы, которые в реальности ничего друг о друге не
знают.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from mcp.server.mcpserver.exceptions import ToolError

from app.adapters.mock.dataset import DEFAULT_SCENARIO, SCENARIOS, Scenario


class ToolFailure(ToolError):
    """Инструмент не смог сделать то, о чём попросили, по причине, которую стоит сообщить.

    Наследование от ToolError из SDK —
    это то, что позволяет сообщению пережить границу протокола: ожидаемый сбой
    возвращается как is_error с целым текстом, а всё остальное трактуется как
    крах, и вызывающему сообщается только имя инструмента. Разница между «такого
    сервиса не существует» и «сервер упал» — ровно то, что нужно агенту для
    маршрутизации.
    """


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse_window(start: str, end: str) -> tuple[datetime, datetime]:
    """Разбирает и проверяет на разумность окно ISO 8601.

    Серверы валидируют свои входные данные. Агент уже проверяет аргументы перед
    отправкой, но MCP-сервер — это публичный интерфейс: вызвать его может всё,
    что говорит на протоколе, поэтому он не может полагаться на благонамеренный
    клиент.
    """
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError as exc:
        raise ToolFailure(f"timestamps must be ISO 8601: {exc}") from exc
    first = first if first.tzinfo else first.replace(tzinfo=UTC)
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    if last < first:
        raise ToolFailure("end must not be earlier than start")
    return first, last


def services_in(scenario: Scenario) -> list[str]:
    return sorted({service for service, _ in scenario.metrics})


def scenario_from_env() -> Scenario:
    """Какой синтетический мир обслуживает этот процесс сервера.

    Вся поверхность конфигурации — одна переменная окружения: эти серверы
    существуют, чтобы их заменили реальными бэкендами, так что всё более сложное
    было бы настройкой того, что всё равно будет удалено.
    """
    name = os.environ.get("MCP_SCENARIO", "")
    return SCENARIOS.get(name, DEFAULT_SCENARIO)
