"""MCP-сервер инцидентов: трекер задач, включая сторону записи.

Это единственный сервер в репозитории, который может что-то изменить, а значит
именно к нему стоит относиться внимательно.

Его защищают два независимых механизма, и ни один из них не промпт.

Сервер заявляет о своём намерении. Каждый инструмент несёт аннотации MCP,
так что клиент может отличить чтение от записи, не распознавая инструмент по
имени. Клиент агента отказывается предоставлять инструмент не только для
чтения, если у запуска нет подтверждения, — сервер, которого он никогда раньше
не видел, всё равно классифицируется правильно.

Сервер обеспечивает свои собственные ограничения. Длина заголовка и тела,
лимит создания на процесс и проверка заголовка на дубликаты. Сервер, который
доверяет своему клиенту вести себя хорошо, — это сервер вообще без свойств
безопасности, а здешним клиентом управляет языковая модель.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.mcp_servers.common import ToolFailure, iso

#: Максимум задач, создаваемых одним процессом сервера. Вышедший из-под контроля
#: агент — реальный сценарий сбоя, а тысяча дублей в трекере имеет цену.
#: Для демонстрационного развёртывания предел намеренно низкий.
MAX_ISSUES_PER_PROCESS = 20
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 20_000


class IssueOut(BaseModel):
    key: str
    title: str
    body: str
    service: str | None = None
    labels: list[str] = Field(default_factory=list)
    state: str = "open"
    created_at: str
    created_by: str
    comments: list[str] = Field(default_factory=list)
    url: str | None = None


class IssueStore:
    """Трекер задач в памяти.

    Отделён от сервера, чтобы тесты могли проверить, что было записано, не
    проходя через протокол, и чтобы реальный трекер мог заменить его, не трогая
    определения инструментов.
    """

    def __init__(self, seed: list[IssueOut] | None = None) -> None:
        self._issues: dict[str, IssueOut] = {i.key: i for i in seed or []}
        self._counter = itertools.count(len(self._issues) + 1)
        self._created = 0

    @property
    def issues(self) -> list[IssueOut]:
        return list(self._issues.values())

    @property
    def created_count(self) -> int:
        return self._created

    def get(self, key: str) -> IssueOut:
        try:
            return self._issues[key]
        except KeyError:
            raise ToolFailure(f"no issue {key!r}") from None

    def search(self, query: str, service: str | None, state: str | None) -> list[IssueOut]:
        needle = query.casefold()
        return [
            issue
            for issue in self._issues.values()
            if (not needle or needle in issue.title.casefold() or needle in issue.body.casefold())
            and (service is None or issue.service == service)
            and (state is None or issue.state == state)
        ]

    def create(
        self, title: str, body: str, service: str | None, labels: list[str], author: str
    ) -> IssueOut:
        if self._created >= MAX_ISSUES_PER_PROCESS:
            raise ToolFailure(
                f"this server has already created {self._created} issues, which is its limit"
            )
        duplicate = any(
            i.title.casefold() == title.casefold() and i.state == "open"
            for i in self._issues.values()
        )
        if duplicate:
            raise ToolFailure(f"an open issue with the title {title!r} already exists")

        key = f"OPS-{next(self._counter)}"
        issue = IssueOut(
            key=key,
            title=title,
            body=body,
            service=service,
            labels=labels,
            created_at=iso(datetime.now(UTC)),
            created_by=author,
            url=f"https://issues.example.com/{key}",
        )
        self._issues[key] = issue
        self._created += 1
        return issue

    def comment(self, key: str, text: str, author: str) -> IssueOut:
        issue = self.get(key)
        issue.comments.append(f"{author}: {text}")
        return issue


def seed_issues() -> list[IssueOut]:
    """Один заранее существующий issue, чтобы поиску было что найти.

    Он также даёт агенту способ заметить, что проблема уже отслеживается, —
    дублирующий отчёт об инциденте хуже, чем никакой.
    """
    return [
        IssueOut(
            key="OPS-1",
            title="Intermittent gateway timeouts in billing-service",
            body=(
                "Occasional GatewayTimeout when the payment provider is slow. "
                "Known, low volume, retried by the client."
            ),
            service="billing-service",
            labels=["billing", "known-issue"],
            state="open",
            created_at="2026-02-04T09:12:00+00:00",
            created_by="d.ivanov",
            url="https://issues.example.com/OPS-1",
        )
    ]


def build_server(store: IssueStore | None = None) -> MCPServer:
    store = store if store is not None else IssueStore(seed_issues())
    server = MCPServer(
        name="ops-incident",
        version="1.0.0",
        instructions=(
            "Issue tracker. Reads are unrestricted; create_issue and "
            "add_issue_comment modify the tracker and are annotated as writes. "
            "Clients must obtain human approval before calling them."
        ),
    )

    @server.tool(
        description=(
            "Search issues by free text, optionally filtered by service and state "
            "('open' or 'closed'). Use it before creating an issue, to avoid filing "
            "a duplicate of something already tracked."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def search_issues(
        query: str = "", service: str | None = None, state: str | None = None
    ) -> list[IssueOut]:
        if state not in (None, "open", "closed"):
            raise ToolFailure("state must be 'open', 'closed', or omitted")
        return store.search(query, service, state)

    @server.tool(
        description="Fetch one issue by key, including its comments.",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_issue(key: str) -> IssueOut:
        return store.get(key)

    @server.tool(
        description=(
            "Create an issue. This modifies the tracker and must not be called "
            "without explicit human approval. Fails if an open issue with the same "
            "title already exists."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False
        ),
    )
    async def create_issue(
        title: str,
        body: str,
        service: str | None = None,
        labels: list[str] | None = None,
        author: str = "ai-operations-agent",
    ) -> IssueOut:
        title = title.strip()
        if not title:
            raise ToolFailure("title must not be empty")
        if len(title) > MAX_TITLE_CHARS:
            raise ToolFailure(f"title must be at most {MAX_TITLE_CHARS} characters")
        if len(body) > MAX_BODY_CHARS:
            raise ToolFailure(f"body must be at most {MAX_BODY_CHARS} characters")
        return store.create(title, body, service, labels or [], author)

    @server.tool(
        description=(
            "Append a comment to an existing issue. This modifies the tracker and "
            "must not be called without explicit human approval."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False
        ),
    )
    async def add_issue_comment(
        key: str, text: str, author: str = "ai-operations-agent"
    ) -> IssueOut:
        if not text.strip():
            raise ToolFailure("comment text must not be empty")
        if len(text) > MAX_BODY_CHARS:
            raise ToolFailure(f"comment must be at most {MAX_BODY_CHARS} characters")
        return store.comment(key, text, author)

    @server.resource(
        "incident://policy",
        name="Write policy",
        description="What this server permits, so a client can check before it asks.",
        mime_type="application/json",
    )
    def policy() -> dict[str, object]:
        return {
            "write_tools": ["create_issue", "add_issue_comment"],
            "requires_human_approval": True,
            "max_issues_per_process": MAX_ISSUES_PER_PROCESS,
            "max_title_chars": MAX_TITLE_CHARS,
            "max_body_chars": MAX_BODY_CHARS,
        }

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
