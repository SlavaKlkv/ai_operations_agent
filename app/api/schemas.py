"""HTTP request/response models. Separate from domain models on purpose: the
wire format is allowed to evolve without dragging the agent's types with it."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agent.state import ApprovalState, RunStatus
from app.domain.models import IncidentAnalysis


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=8, max_length=2000, description="What to investigate, in prose.")
    target_service: str | None = Field(
        default=None,
        max_length=200,
        description="Optional override; otherwise inferred from the task.",
    )


class ToolCallView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tool: str
    arguments: dict
    started_at: datetime
    duration_ms: float
    attempt: int
    ok: bool
    cached: bool = False
    error: str | None = None
    result_summary: str = ""


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task: str
    target_service: str | None
    model_name: str | None = None
    status: RunStatus
    approval_state: ApprovalState
    step_count: int
    tool_call_count: int
    total_tokens: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    final_result: str | None = None


class PendingApproval(BaseModel):
    """The write a run is waiting on. Shown in full: a reviewer approves
    content, not a description of content."""

    model_config = ConfigDict(from_attributes=True)

    approval_id: uuid.UUID
    tool: str
    arguments: dict
    rationale: str = ""


class ApprovalDecision(BaseModel):
    """A human decision: a yes or a no, and optionally why.

    Deliberately unable to express *what* to do — the action is whatever the
    graph checkpointed, so an approval cannot be redirected onto content the
    reviewer never saw. And deliberately unable to say *who* — identity comes
    from the credential, because a name in a request body is a label, not an
    identity.
    """

    model_config = ConfigDict(extra="forbid")

    approved: bool
    note: str = Field(default="", max_length=2000, description="Why, for the audit trail.")


class RunDetail(RunSummary):
    analysis: IncidentAnalysis | None = None
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    pending_approval: PendingApproval | None = None
    approved_by: str | None = None
    action_result: dict | None = None


class TraceStep(BaseModel):
    """One observable thing the workflow did."""

    step: int
    node: str
    detail: dict = Field(default_factory=dict)


class RunTrace(BaseModel):
    """The replayable story of a run: nodes, tools, branches and failures."""

    run_id: uuid.UUID
    status: RunStatus
    steps: list[TraceStep] = Field(default_factory=list)
    tool_calls: list[ToolCallView] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    durable_approvals: bool = False
    authentication: bool = True
    storage_backend: str
    cache_backend: str
    checkpointer: str
