"""Реализации провайдеров, получающие свои данные по MCP.

Здесь и живёт архитектурный выбор V3. MCP — это слой интеграции, а не замена
контракта инструментов агента. Агент сохраняет свой типизированный реестр,
свои защитные ограничения и свои ограниченные представления; меняется лишь то,
откуда приходят данные.

Конкретно: эти классы реализуют ровно те протоколы из app.adapters.base,
что и mock-провайдеры, поэтому build_registry(monitoring, code, logs) не
меняется, каждый инструмент сохраняет свою схему Pydantic, а замена mock на MCP
— это решение о проводке. Если бы инструменты агента генерировались из того,
что рекламируют серверы, сервер мог бы расширить доступ агента, отредактировав
свой собственный манифест.

Всё, что пересекает границу, проходит повторную валидацию. Ответ удалённого
сервера — это недоверенный ввод: здесь он разбирается в модель предметной
области, а ответ, который не подходит, — это отказ на границе, а не странное
значение, всплывающее тремя слоями позже.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError

from app.domain.models import (
    Alert,
    AlertSeverity,
    ChangedFile,
    Commit,
    Deployment,
    ErrorGroup,
    Issue,
    IssueDraft,
    IssueState,
    MetricPoint,
    MetricSeries,
    PullRequest,
    RunbookHit,
)
from app.mcp.client import MCPToolPool, ToolCallFailed

log = structlog.get_logger(__name__)

#: Единицы измерения каждой метрики. Серверы возвращают значения без единиц;
#: явное описание здесь сохраняет корректность отображения без изменения протокола.
METRIC_UNITS = {
    "error_rate": "ratio",
    "request_rate": "rps",
    "latency_p50": "seconds",
    "latency_p95": "seconds",
    "latency_p99": "seconds",
}


class RemoteDataError(RuntimeError):
    """Сервер ответил, но результатом, который эта система не может использовать."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _unwrap(payload: dict[str, Any]) -> Any:
    """MCP оборачивает возвращаемое значение, не являющееся объектом, в {"result": ...}.

    Инструмент, возвращающий список, приходит обёрнутым; инструмент,
    возвращающий модель, — нет. Нормализация здесь избавляет все места вызовов
    от этой детали протокола.
    """
    if set(payload) == {"result"}:
        return payload["result"]
    return payload


class MCPMonitoringProvider:
    """Метрики, оповещения и агрегированные ошибки через сервер мониторинга."""

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def get_service_metrics(
        self, service: str, metric: str, start: datetime, end: datetime
    ) -> MetricSeries:
        payload = _unwrap(
            await self._pool.call(
                "get_service_metrics",
                {"service": service, "metric": metric, "start": _iso(start), "end": _iso(end)},
            )
        )
        try:
            points = tuple(
                MetricPoint(timestamp=_at(p["at"]), value=float(p["value"]))
                for p in payload.get("points", [])
            )
            return MetricSeries(
                service=payload["service"],
                metric=payload["metric"],
                unit=payload.get("unit") or METRIC_UNITS.get(metric, ""),
                points=points,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed metrics for {service}/{metric}: {exc}") from exc

    async def get_recent_alerts(self, service: str, start: datetime, end: datetime) -> list[Alert]:
        payload = _unwrap(
            await self._pool.call(
                "get_recent_alerts",
                {"service": service, "start": _iso(start), "end": _iso(end)},
            )
        )
        try:
            return [
                Alert(
                    name=a["name"],
                    service=a["service"],
                    severity=AlertSeverity(a["severity"]),
                    fired_at=_at(a["fired_at"]),
                    resolved_at=_at(a["resolved_at"]) if a.get("resolved_at") else None,
                    description=a.get("description", ""),
                )
                for a in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed alerts for {service}: {exc}") from exc


class MCPLogProvider:
    """Агрегированные ошибки приложения через сервер мониторинга.

    Отдельный от мониторинга провайдер, хотя оба обслуживает один сервер: у
    агента инструмент логов и инструмент метрик — независимые возможности, и
    привязка их к одному классу лишила бы нас возможности позже направить их на
    разные бэкенды — а это обычное конечное состояние.
    """

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def get_error_groups(
        self, service: str, start: datetime, end: datetime, min_count: int = 1
    ) -> list[ErrorGroup]:
        payload = _unwrap(
            await self._pool.call(
                "get_error_groups",
                {
                    "service": service,
                    "start": _iso(start),
                    "end": _iso(end),
                    "min_count": min_count,
                },
            )
        )
        try:
            return [
                ErrorGroup(
                    error_type=g["error_type"],
                    count=int(g["count"]),
                    first_seen=_at(g["first_seen"]),
                    last_seen=_at(g["last_seen"]),
                    sample_message=g.get("sample_message", ""),
                    stack_top=g.get("stack_top"),
                    services=tuple(g.get("services", ())),
                )
                for g in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed error groups for {service}: {exc}") from exc


class MCPCodeProvider:
    """Деплои, коммиты и pull request через сервер кода."""

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def get_recent_deployments(
        self, service: str, start: datetime, end: datetime
    ) -> list[Deployment]:
        payload = _unwrap(
            await self._pool.call(
                "get_recent_deployments",
                {"service": service, "start": _iso(start), "end": _iso(end)},
            )
        )
        try:
            return [
                Deployment(
                    service=d["service"],
                    version=d["version"],
                    environment=d.get("environment", "production"),
                    deployed_at=_at(d["deployed_at"]),
                    commit_sha=d["commit_sha"],
                    deployed_by=d.get("deployed_by"),
                )
                for d in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed deployments for {service}: {exc}") from exc

    async def get_commits(self, service: str, since: datetime, until: datetime) -> list[Commit]:
        payload = _unwrap(
            await self._pool.call(
                "get_commits",
                {"service": service, "start": _iso(since), "end": _iso(until)},
            )
        )
        try:
            return [
                Commit(
                    sha=c["sha"],
                    message=c["message"],
                    author=c["author"],
                    committed_at=_at(c["committed_at"]),
                    files=tuple(
                        ChangedFile(
                            path=f["path"],
                            additions=int(f.get("additions", 0)),
                            deletions=int(f.get("deletions", 0)),
                        )
                        for f in c.get("files", ())
                    ),
                )
                for c in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed commits for {service}: {exc}") from exc

    async def get_pull_request(self, service: str, number: int) -> PullRequest | None:
        """Отсутствующий pull request — это ответ, а не сбой.

        Сервер сообщает о «нет такого PR» как об ошибке инструмента, потому что
        на уровне протокола это так и есть; на уровне предметной области это
        просто None, и агент не должен отличать это от недоступности сервера.
        """
        try:
            payload = _unwrap(
                await self._pool.call("get_pull_request", {"service": service, "number": number})
            )
        except ToolCallFailed as exc:
            log.info("mcp.pull_request_missing", number=number, detail=str(exc))
            return None
        try:
            return PullRequest(
                number=int(payload["number"]),
                title=payload["title"],
                author=payload["author"],
                merged_at=_at(payload["merged_at"]) if payload.get("merged_at") else None,
                commits=tuple(payload.get("commits", ())),
                url=payload.get("url"),
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed pull request #{number}: {exc}") from exc


class MCPKnowledgeProvider:
    """Поиск ранбуков через сервер знаний.

    Не входит в app.adapters.base: знания — это возможность, которую
    агент получил вместе с MCP, а не та, что уже была в детерминированном
    рабочем процессе, поэтому он получает собственный протокол, вместо того
    чтобы встраиваться в существующий.
    """

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def search_runbooks(
        self, query: str, service: str | None = None, limit: int = 3
    ) -> list[RunbookHit]:
        payload = _unwrap(
            await self._pool.call(
                "search_runbooks", {"query": query, "service": service, "limit": limit}
            )
        )
        if not isinstance(payload, list):
            raise RemoteDataError("runbook search did not return a list of hits")
        try:
            return [
                RunbookHit(
                    doc_id=item["doc_id"],
                    title=item["title"],
                    excerpt=item["excerpt"],
                    score=float(item["score"]),
                    services=tuple(item.get("services", ())),
                    tags=tuple(item.get("tags", ())),
                )
                for item in payload
            ]
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise RemoteDataError(f"malformed runbook search: {exc}") from exc


class MCPIssueProvider:
    """Трекер задач через сервер инцидентов — чтение и запись.

    Флаг approved передаётся в пул, а не хранится в этом объекте. Провайдер,
    который можно было бы создать «в режиме записи», проносил бы это разрешение
    в каждый последующий вызов; передача его на каждый вызов привязывает
    подтверждение к единственному действию, для которого оно выдано.
    """

    def __init__(self, pool: MCPToolPool) -> None:
        self._pool = pool

    async def search_issues(
        self, query: str, service: str | None = None, state: str | None = None
    ) -> list[Issue]:
        payload = _unwrap(
            await self._pool.call(
                "search_issues", {"query": query, "service": service, "state": state}
            )
        )
        return [_issue(item) for item in payload]

    async def get_issue(self, key: str) -> Issue | None:
        try:
            return _issue(_unwrap(await self._pool.call("get_issue", {"key": key})))
        except ToolCallFailed:
            return None

    async def create_issue(self, draft: IssueDraft, *, author: str) -> Issue:
        payload = _unwrap(
            await self._pool.call(
                "create_issue",
                {
                    "title": draft.title,
                    "body": draft.body,
                    "service": draft.service,
                    "labels": draft.labels,
                    "author": author,
                },
                approved=True,
            )
        )
        return _issue(payload)

    async def add_issue_comment(self, key: str, text: str, *, author: str) -> Issue:
        payload = _unwrap(
            await self._pool.call(
                "add_issue_comment",
                {"key": key, "text": text, "author": author},
                approved=True,
            )
        )
        return _issue(payload)


def _issue(payload: dict[str, Any]) -> Issue:
    try:
        return Issue(
            key=payload["key"],
            title=payload["title"],
            body=payload.get("body", ""),
            service=payload.get("service"),
            labels=tuple(payload.get("labels", ())),
            state=IssueState(payload.get("state", "open")),
            created_at=_at(payload["created_at"]) if payload.get("created_at") else None,
            created_by=payload.get("created_by", ""),
            comments=tuple(payload.get("comments", ())),
            url=payload.get("url"),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise RemoteDataError(f"malformed issue payload: {exc}") from exc
