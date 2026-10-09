"""MCP-клиент: один объект, владеющий всеми соединениями с внешней системой.

Агент никогда не держит MCP-сессию. Он просит у этого пула вызвать инструмент
и получает обратно обычный, провалидированный dict — либо типизированный
отказ. Именно поэтому здесь сосредоточены три вещи:

Соединения пулятся и открываются лениво. Запуск четырёх подпроцессов на
каждое расследование занял бы основную часть задержки запуска, который иначе
занимает миллисекунды.

Мёртвый сервер — это деградация запуска, а не крах. Каждый сервер помечен как
обязательный или необязательный; необязательный, который не запускается,
фиксируется и пропускается, а обязательный проваливает вызвавший его вызов —
но не процесс.

Чтение и запись определяются по аннотациям самого сервера. Пул читает
read_only_hint у каждого обнаруженного инструмента и отказывается вызывать
инструмент не только для чтения, если вызывающая сторона не передала явный
токен подтверждения. Сервер, которого агент никогда раньше не видел, всё равно
классифицируется правильно, потому что классификация исходит из протокола, а не
из жёстко зашитого списка имён.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from typing import Any

import structlog
from mcp import Client, StdioServerParameters
from mcp.types import Tool

from app.mcp.config import ServerSpec, Transport, default_servers

log = structlog.get_logger(__name__)


class MCPError(RuntimeError):
    """На слое интеграции что-то пошло не так."""


class ServerUnavailable(MCPError):
    """Сервер недоступен или не запустился."""


class ToolCallFailed(MCPError):
    """Сервер выполнил инструмент и сообщил о сбое."""


class WriteNotPermitted(MCPError):
    """Инструмент записи был вызван без токена подтверждения."""


class UnknownTool(MCPError):
    """Ни один подключённый сервер не предлагает инструмент с таким именем."""


@dataclass(frozen=True, slots=True)
class RemoteTool:
    """Инструмент в том виде, как его описывает сервер, плюс источник."""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.name}"


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Что произошло при попытке использовать сервер — для эндпоинта здоровья."""

    name: str
    connected: bool
    required: bool
    tool_count: int = 0
    error: str | None = None


def _read_only(tool: Tool) -> bool:
    """Отсутствие аннотации означает «считаем, что он пишет».

    Если бы инструмент без аннотации по умолчанию считался доступным только для
    чтения, безопасный путь зависел бы от того, вспомнил ли сервер заявить о
    себе, — а именно такого допущения граница безопасности делать не должна.
    """
    annotations = tool.annotations
    return bool(annotations and annotations.read_only_hint)


class MCPToolPool:
    """Соединения со всеми настроенными серверами, открываемые при первом использовании."""

    def __init__(self, specs: tuple[ServerSpec, ...] | None = None) -> None:
        self._specs = {spec.name: spec for spec in (specs or default_servers())}
        self._clients: dict[str, Client] = {}
        self._tools: dict[str, RemoteTool] = {}
        self._status: dict[str, ServerStatus] = {}
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._startup_error: BaseException | None = None

    async def __aenter__(self) -> MCPToolPool:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ── Жизненный цикл ───────────────────────────────────────────────────────

    async def connect(self) -> tuple[ServerStatus, ...]:
        """Открывает каждый настроенный сервер один раз, терпимо относясь к тем, что падают.

        Соединения открываются и закрываются внутри одной выделенной задачи, а не
        inline. Это не стилистический выбор: транспорты построены на cancel scope
        из anyio, которые должна покинуть та же задача, что в них вошла. Открытие
        в одной задаче и закрытие в другой — ровно то, что рано или поздно сделают
        ASGI lifespan, разбор тестовой фикстуры или любой asyncio.gather, —
        приводит к ошибке «попытка выйти из cancel scope в другой задаче».
        Владение scope'ами здесь делает пул безопасным для открытия и закрытия
        откуда угодно.
        """
        async with self._lock:
            if self._owner is not None:
                return self.status
            self._ready.clear()
            self._closing.clear()
            self._startup_error = None
            self._owner = asyncio.create_task(self._own_connections(), name="mcp-pool")
            await self._ready.wait()
            if self._startup_error is not None:
                await self._shutdown()
                raise _flatten(self._startup_error)
        return self.status

    async def _own_connections(self) -> None:
        """Держит каждое соединение открытым, пока aclose не скажет иначе."""
        try:
            async with AsyncExitStack() as stack:
                for spec in self._specs.values():
                    await self._connect_one(stack, spec)
                self._ready.set()
                await self._closing.wait()
        except BaseException as exc:  # повторно выбрасывается вызывающей стороне из connect()
            self._startup_error = exc
        finally:
            # connect() должен разблокироваться при любом исходе, включая сбой
            # до открытия серверов, иначе неверная конфигурация вызовет зависание.
            self._ready.set()

    async def _connect_one(self, stack: AsyncExitStack, spec: ServerSpec) -> None:
        try:
            client = await asyncio.wait_for(
                stack.enter_async_context(_client_for(spec)), timeout=spec.timeout_seconds
            )
            listed = await asyncio.wait_for(client.list_tools(), timeout=spec.timeout_seconds)
        except Exception as exc:
            # Для запуска все ошибки старта означают одно: сервер недоступен.
            # Поэтому они обрабатываются вместе и различаются записанным сообщением,
            # а не ветвлением потока управления.
            message = f"{type(exc).__name__}: {exc}"
            log.warning("mcp.server_unavailable", server=spec.name, error=message)
            self._status[spec.name] = ServerStatus(
                name=spec.name, connected=False, required=spec.required, error=message
            )
            return

        self._clients[spec.name] = client
        discovered = 0
        for tool in listed.tools:
            if not spec.permits(tool.name):
                log.info("mcp.tool_filtered", server=spec.name, tool=tool.name)
                continue
            if tool.name in self._tools:
                # Одинаковое имя от двух серверов — ошибка конфигурации, которую
                # нельзя разрешать догадкой о том, какой сервер имелся в виду.
                raise MCPError(
                    f"tool {tool.name!r} is offered by both "
                    f"{self._tools[tool.name].server!r} and {spec.name!r}"
                )
            self._tools[tool.name] = RemoteTool(
                server=spec.name,
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema or {},
                read_only=_read_only(tool),
            )
            discovered += 1

        self._status[spec.name] = ServerStatus(
            name=spec.name, connected=True, required=spec.required, tool_count=discovered
        )
        log.info("mcp.server_connected", server=spec.name, tools=discovered)

    async def aclose(self) -> None:
        async with self._lock:
            await self._shutdown()

    async def _shutdown(self) -> None:
        owner, self._owner = self._owner, None
        self._closing.set()
        if owner is not None:
            with suppress(Exception):
                await owner
        self._clients.clear()
        self._tools.clear()

    # ── Интроспекция ─────────────────────────────────────────────────────────

    @property
    def status(self) -> tuple[ServerStatus, ...]:
        return tuple(
            self._status.get(name, ServerStatus(name=name, connected=False, required=spec.required))
            for name, spec in self._specs.items()
        )

    @property
    def healthy(self) -> bool:
        """Ложь, когда отсутствует сервер, без которого запуск не может обойтись."""
        return all(s.connected for s in self.status if s.required)

    def tools(self, *, read_only: bool | None = None) -> tuple[RemoteTool, ...]:
        found = tuple(self._tools.values())
        if read_only is None:
            return found
        return tuple(t for t in found if t.read_only is read_only)

    def get(self, name: str) -> RemoteTool:
        try:
            return self._tools[name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "<none connected>"
            raise UnknownTool(f"no MCP tool {name!r}; available: {known}") from None

    async def read_resource(self, server: str, uri: str) -> str:
        client = self._clients.get(server)
        if client is None:
            raise ServerUnavailable(f"server {server!r} is not connected")
        result = await client.read_resource(uri)
        return "\n".join(getattr(c, "text", "") for c in result.contents)

    # ── Вызовы ───────────────────────────────────────────────────────────────

    async def call(
        self, name: str, arguments: dict[str, Any], *, approved: bool = False
    ) -> dict[str, Any]:
        """Вызывает инструмент по имени и возвращает его структурированный результат.

        approved — единственное, что разблокирует запись, и это параметр,
        а не состояние экземпляра, чтобы разрешение выдавалось на каждый вызов.
        Пул, который можно было бы «перевести в режим записи», так в нём и
        остался бы.
        """
        await self.connect()
        tool = self.get(name)
        if not tool.read_only and not approved:
            raise WriteNotPermitted(
                f"{tool.qualified_name} is not annotated read-only and no approval was given"
            )

        spec = self._specs[tool.server]
        client = self._clients.get(tool.server)
        if client is None:
            raise ServerUnavailable(f"server {tool.server!r} is not connected")

        try:
            result = await asyncio.wait_for(
                client.call_tool(name, arguments), timeout=spec.timeout_seconds
            )
        except TimeoutError as exc:
            raise ServerUnavailable(
                f"{tool.qualified_name} timed out after {spec.timeout_seconds}s"
            ) from exc
        except Exception as exc:
            raise ServerUnavailable(f"{tool.qualified_name}: {type(exc).__name__}: {exc}") from exc

        if result.is_error:
            raise ToolCallFailed(f"{tool.qualified_name}: {_text_of(result)}")

        structured = result.structured_content
        if structured is None:
            raise ToolCallFailed(
                f"{tool.qualified_name} returned no structured content; "
                "this integration requires tools that declare an output schema"
            )
        return structured


def _flatten(exc: BaseException) -> BaseException:
    """Разворачивает ExceptionGroup, через который пробрасывают ошибки группы задач anyio.

    Ошибка конфигурации должна всплывать как описывающая её ошибка, а не как
    ExceptionGroup, в котором вызывающей стороне приходится копаться, — группа
    это артефакт того, как надзирают за транспортами, а не информация.
    """
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _client_for(spec: ServerSpec) -> Client:
    match spec.transport:
        case Transport.STDIO:
            if not spec.command:
                raise MCPError(f"server {spec.name!r} has no command to launch")
            return Client(
                StdioServerParameters(
                    command=spec.command[0], args=list(spec.command[1:]), env=spec.env or None
                )
            )
        case Transport.HTTP:
            if not spec.url:
                raise MCPError(f"server {spec.name!r} has no url")
            return Client(spec.url)
        case Transport.IN_PROCESS:
            if spec.factory is None:
                raise MCPError(f"server {spec.name!r} has no factory")
            return Client(spec.factory())


def _text_of(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content).strip() or "no detail"
