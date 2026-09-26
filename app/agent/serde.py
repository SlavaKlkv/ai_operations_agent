"""How workflow state is written to, and read back from, a checkpoint.

The default LangGraph serializer will reconstruct any type it finds in a
checkpoint. That is convenient and it is a real weakness: a checkpoint row is
data in a database, and anything able to write to that database could choose
what gets instantiated when the row is read back. The library says so in its
own security note.

So the allowlist here is explicit. These are the types the agent's state is
made of; a checkpoint containing anything else fails to deserialise instead of
being trusted. The list is short because the state is deliberately small, and
keeping it accurate is the cost of that guarantee — a new state type that is
not added here will surface immediately as a failed resume, not as a silent
security hole.
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
    """A serializer that will only rebuild this application's own types."""
    return JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES)
