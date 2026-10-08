"""Как состояние рабочего процесса записывается в чекпоинт и читается обратно.

Сериализатор LangGraph по умолчанию восстановит любой тип, найденный в
чекпоинте. Это удобно и это настоящая слабость: строка чекпоинта — это данные
в базе, и любой, кто может писать в эту базу, мог бы выбрать, что будет
создано при чтении строки обратно. Библиотека говорит об этом в собственной
заметке о безопасности.

Поэтому allowlist здесь явный. Это типы, из которых состоит состояние агента;
чекпоинт, содержащий что-либо ещё, не десериализуется, а не принимается на
веру. Список короткий, потому что состояние намеренно маленькое, и поддержание
его точности — цена этой гарантии: новый тип состояния, не добавленный сюда,
сразу проявится как неудачное возобновление, а не как тихая дыра в
безопасности.
"""

from __future__ import annotations

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agent.state import (
    AgentState,
    ApprovalState,
    CollectedContext,
    ProposedAction,
    RunError,
    RunStatus,
    ToolCallRecord,
)
from app.agent.tools.base import ToolRequest
from app.domain.models import (
    Alert,
    AlertSeverity,
    ChangedFile,
    Commit,
    Deployment,
    ErrorGroup,
    Evidence,
    EvidenceKind,
    Hypothesis,
    IncidentAnalysis,
    Issue,
    IssueDraft,
    IssueState,
    LogEvent,
    LogLevel,
    MetricPoint,
    MetricSeries,
    PullRequest,
)

#: Все типы, которые допустимы в сохранённом AgentState.
CHECKPOINT_TYPES: tuple[type, ...] = (
    # Состояние воркфлоу
    AgentState,
    ApprovalState,
    CollectedContext,
    ProposedAction,
    RunError,
    RunStatus,
    ToolCallRecord,
    ToolRequest,
    # Предметная область
    Alert,
    AlertSeverity,
    ChangedFile,
    Commit,
    Deployment,
    ErrorGroup,
    Evidence,
    EvidenceKind,
    Hypothesis,
    IncidentAnalysis,
    Issue,
    IssueDraft,
    IssueState,
    LogEvent,
    LogLevel,
    MetricPoint,
    MetricSeries,
    PullRequest,
)


def agent_serializer() -> JsonPlusSerializer:
    """Сериализатор, который восстановит только собственные типы этого приложения."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
