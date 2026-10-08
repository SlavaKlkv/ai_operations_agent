"""Единственный MCP-пул приложения, привязанный ко времени жизни процесса.

Один пул на процесс, открывается при старте и закрывается при остановке.
Причина — стоимость: каждый stdio-сервер это подпроцесс, и открытие четырёх из
них на каждое расследование заняло бы большую часть задержки запуска. Безопасно
это потому, что пул владеет своими соединениями в выделенной задаче — см.
MCPToolPool.connect.

Тесты и оценочный стенд строят собственные пулы и никогда не трогают этот
модуль, поэтому граф принимает провайдеров, а не обращается к глобальной
переменной.
"""

from __future__ import annotations

from app.mcp.client import MCPToolPool
from app.mcp.config import ServerSpec

_pool: MCPToolPool | None = None


async def startup(specs: tuple[ServerSpec, ...] | None = None) -> MCPToolPool:
    """Открывает пул. Безопасно вызывать дважды; второй вызов ничего не делает."""
    global _pool
    if _pool is None:
        _pool = MCPToolPool(specs)
        await _pool.connect()
    return _pool


async def shutdown() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def get_pool() -> MCPToolPool:
    """Зависимость FastAPI. Создание пула лениво здесь, а не возбуждение исключения,
    не даёт запросу упасть лишь из-за того, что изменился порядок запуска."""
    global _pool
    if _pool is None:
        _pool = MCPToolPool()
    return _pool
