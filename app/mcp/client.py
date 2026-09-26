"""The MCP client: one object that owns every connection to an external system.

The agent never holds an MCP session. It asks this pool for a tool call and
gets back a plain, validated dict — or a typed failure. Three things are
concentrated here for that reason:

*Connections are pooled and lazily opened.* Launching four subprocesses per
investigation would dominate the latency of a run that otherwise takes
milliseconds.

*A dead server is a degraded run, not a crashed one.* Each server is marked
required or optional; an optional one that will not start is recorded and
skipped, and a required one fails the call that needed it — not the process.

*Read and write are decided from the server's own annotations.* The pool reads
``read_only_hint`` off each discovered tool and refuses to call a non-read-only
tool unless the caller passes an explicit approval token. A server the agent
has never seen before is still classified correctly, because the classification
comes from the protocol rather than from a hardcoded list of names.
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
    """Something went wrong at the integration layer."""


class ServerUnavailable(MCPError):
    """The server could not be reached or would not start."""


class ToolCallFailed(MCPError):
    """The server ran the tool and reported a failure."""


class WriteNotPermitted(MCPError):
    """A write tool was called without an approval token."""


class UnknownTool(MCPError):
    """No connected server offers a tool by that name."""


@dataclass(frozen=True, slots=True)
class RemoteTool:
    """A tool as the server describes it, plus where it came from."""

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
    """What happened when we tried to use a server — for the health endpoint."""

    name: str
    connected: bool
    required: bool
    tool_count: int = 0
    error: str | None = None


def _read_only(tool: Tool) -> bool:
    """Absence of an annotation means "assume it writes".

    Defaulting an unannotated tool to read-only would make the safe path
    depend on a server remembering to declare itself, which is exactly the
    assumption a security boundary must not make.
    """
    annotations = tool.annotations
    return bool(annotations and annotations.read_only_hint)


class MCPToolPool:
    """Connections to every configured server, opened on first use."""

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

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> tuple[ServerStatus, ...]:
        """Open every configured server once, tolerating the ones that fail.

        The connections are opened and closed inside a single dedicated task
        rather than inline. That is not a stylistic choice: the transports are
        built on anyio cancel scopes, which must be exited by the same task
        that entered them. Opening in one task and closing in another — which
        is exactly what an ASGI lifespan, a test fixture teardown, or any
        ``asyncio.gather`` will eventually do — raises "attempted to exit
        cancel scope in a different task". Owning the scopes here makes the
        pool safe to open and close from anywhere.
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
        """Hold every connection open until :meth:`aclose` says otherwise."""
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

    # ── Introspection ────────────────────────────────────────────────────────

    @property
    def status(self) -> tuple[ServerStatus, ...]:
        return tuple(
            self._status.get(name, ServerStatus(name=name, connected=False, required=spec.required))
            for name, spec in self._specs.items()
        )

    @property
    def healthy(self) -> bool:
        """False when a server the run cannot do without is missing."""
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

    # ── Calling ──────────────────────────────────────────────────────────────

    async def call(
        self, name: str, arguments: dict[str, Any], *, approved: bool = False
    ) -> dict[str, Any]:
        """Call a tool by name and return its structured result.

        ``approved`` is the only thing that unlocks a write, and it is a
        parameter rather than instance state so that permission is granted per
        call. A pool that could be "put into write mode" would stay in it.
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
    """Unwrap the ExceptionGroup anyio task groups raise through.

    A configuration mistake should surface as the error that describes it,
    not as an ``ExceptionGroup`` the caller has to dig through — the group is
    an artefact of how the transports are supervised, not information.
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
