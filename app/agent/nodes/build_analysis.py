"""Assemble the structured IncidentAnalysis from state.

Two paths produce the same schema. Without a model, the report is rendered
from evidence by plain code and every sentence is traceable by construction.
With a model, the wording and the reasoning about which cause fits best are
the model's — but the evidence list is not.

That distinction is the whole grounding strategy. The model is asked for
:class:`AnalysisDraft`, which contains no evidence field at all; the evidence
attached to the finished analysis is the evidence the tools actually returned.
A model cannot cite a metric it was never shown, because it is not the thing
writing the citations. What it *can* do — claim something the evidence does
not support — is caught separately, by checking that the references it names
exist before the draft is accepted.
"""

from __future__ import annotations

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from app.agent.llm import LLMError, Usage, structured
from app.agent.state import AgentState, RunError, RunStatus
from app.domain.models import Confidence, EvidenceKind, Hypothesis, IncidentAnalysis

log = structlog.get_logger(__name__)

#: Ниже этого порога агент честно сообщает о неопределённости.
CONFIDENCE_FLOOR = 0.6

SYSTEM_PROMPT = """\
You are writing the conclusion of an incident investigation for the engineers \
who will act on it. You are given the evidence that automated tools collected \
and the correlations that deterministic code already computed.

Rules:
- Every claim must be supported by the evidence listed. If the evidence does \
not establish a cause, say that plainly and give a low confidence.
- Temporal coincidence is not causation. A deployment shortly before a spike \
is a candidate; a deployment that changed the code in the failing stack frame \
is a finding. Distinguish the two.
- Confidence above 0.85 requires evidence connecting a specific change to the \
specific failure, not just timing.
- Recommended actions must be things an on-call engineer can do now.
- Be concise. No preamble, no restating the task.
"""


class AnalysisDraft(BaseModel):
    """What the model is allowed to decide.

    Deliberately missing an ``evidence`` field: citations are attached from
    state, never generated. See the module docstring.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(
        max_length=600, description="One or two sentences: what happened and, if known, why."
    )
    symptoms: list[str] = Field(
        default_factory=list, max_length=8, description="Observable effects, most severe first."
    )
    suspected_causes: list[Hypothesis] = Field(
        default_factory=list,
        max_length=4,
        description="Candidate causes, each with a confidence and the evidence references "
        "that support or contradict it.",
    )
    recommended_actions: list[str] = Field(
        default_factory=list, max_length=6, description="Concrete next steps for an engineer."
    )
    confidence: Confidence = Field(
        default=0.0, description="Overall confidence in the leading cause."
    )


def make_build_analysis_node(model: BaseChatModel | None = None):
    """Build the node. Without a model it renders the report deterministically."""

    async def build_analysis_node(state: AgentState) -> AgentState:
        evidence = list(state.get("evidence", []))
        hypotheses = list(state.get("hypotheses", []))
        service = state.get("target_service") or "unknown"
        errors: list[RunError] = []

        draft: AnalysisDraft | None = None
        usage = Usage()
        if model is not None and evidence:
            try:
                draft, usage = await structured(
                    model,
                    AnalysisDraft,
                    [
                        SystemMessage(content=SYSTEM_PROMPT),
                        HumanMessage(content=render_findings(state)),
                    ],
                )
            except LLMError as exc:
                log.warning("analysis.llm_failed", error=str(exc))
                errors.append(
                    RunError(
                        node="generate_analysis",
                        kind="llm_failed",
                        message=str(exc),
                        recoverable=True,
                    )
                )
            else:
                draft = _drop_ungrounded(draft, {e.reference for e in evidence})

        analysis = (
            _from_draft(state, draft) if draft is not None else _deterministic(state, hypotheses)
        )
        return AgentState(
            current_step="generate_analysis",
            step_count=state.get("step_count", 0) + 1,
            analysis=analysis,
            status=RunStatus.RUNNING,
            errors=errors,
            llm_calls=1 if draft is not None else 0,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            observations=[
                {
                    "node": "generate_analysis",
                    "authored_by": "model" if draft is not None else "deterministic",
                    "confidence": analysis.confidence,
                    "service": service,
                }
            ],
        )

    return build_analysis_node


#: Сохранено для прямого использования детерминированного узла существующим кодом и тестами.
build_analysis_node = make_build_analysis_node(None)


# ── Grounding ────────────────────────────────────────────────────────────────


def _drop_ungrounded(draft: AnalysisDraft, references: set[str]) -> AnalysisDraft:
    """Remove citations that do not correspond to collected evidence.

    A hypothesis left with no supporting evidence keeps its statement but
    loses its claim to confidence — it becomes a lead, not a finding. Silently
    trusting an invented reference is exactly the failure this system exists
    to avoid.
    """
    cleaned: list[Hypothesis] = []
    for hypothesis in draft.suspected_causes:
        supporting = tuple(r for r in hypothesis.supporting_evidence if r in references)
        dropped = len(hypothesis.supporting_evidence) - len(supporting)
        if dropped:
            log.warning(
                "analysis.ungrounded_reference",
                statement=hypothesis.statement[:120],
                dropped=dropped,
            )
        cleaned.append(
            hypothesis.model_copy(
                update={
                    "supporting_evidence": supporting,
                    "contradicting_evidence": tuple(
                        r for r in hypothesis.contradicting_evidence if r in references
                    ),
                    "confidence": min(hypothesis.confidence, 0.4)
                    if not supporting
                    else hypothesis.confidence,
                }
            )
        )
    top = max((h.confidence for h in cleaned), default=draft.confidence)
    return draft.model_copy(
        update={"suspected_causes": cleaned, "confidence": min(draft.confidence, top)}
    )


def _from_draft(state: AgentState, draft: AnalysisDraft) -> IncidentAnalysis:
    evidence = list(state.get("evidence", []))
    return IncidentAnalysis(
        service=state.get("target_service") or "unknown",
        incident_start=_incident_start(state),
        symptoms=draft.symptoms,
        suspected_causes=sorted(draft.suspected_causes, key=lambda h: h.confidence, reverse=True),
        evidence=evidence,
        confidence=draft.confidence,
        recommended_actions=draft.recommended_actions,
        requires_human_review=True,
        summary=draft.summary,
    )


# ── Детерминированный сценарий ───────────────────────────────────────────────


def _deterministic(state: AgentState, hypotheses: list[Hypothesis]) -> IncidentAnalysis:
    evidence = list(state.get("evidence", []))
    service = state.get("target_service") or "unknown"
    context = state.get("context")

    symptoms = [e.summary for e in evidence if e.kind in (EvidenceKind.METRIC, EvidenceKind.ALERT)]
    symptoms += [e.summary for e in evidence if e.kind is EvidenceKind.LOG]

    best = max(hypotheses, key=lambda h: h.confidence, default=None)
    confidence = best.confidence if best else 0.0

    return IncidentAnalysis(
        service=service,
        incident_start=_incident_start(state),
        symptoms=symptoms,
        suspected_causes=sorted(hypotheses, key=lambda h: h.confidence, reverse=True),
        evidence=evidence,
        confidence=confidence,
        recommended_actions=_recommended_actions(context, best),
        requires_human_review=True,
        summary=_summary(service, best, confidence),
    )


def _incident_start(state: AgentState):
    return next(
        (e.observed_at for e in state.get("evidence", []) if e.source_tool == "detect_spike"), None
    )


def _recommended_actions(context, best) -> list[str]:
    if best is None or best.confidence < CONFIDENCE_FLOOR:
        return [
            "Widen the investigation window and re-run: the current evidence does not "
            "identify a cause with enough confidence to act on."
        ]
    actions = []
    if context and context.deployments:
        suspect = context.deployments[0]
        actions.append(f"Roll back {suspect.service} to the previous release and confirm recovery.")
    if context and context.commits:
        actions.append(
            f"Review {context.commits[0].short_sha} — {context.commits[0].message} — "
            "for the unhandled case visible in the logs."
        )
    actions.append("Add a regression test covering the failing code path before re-deploying.")
    return actions


def _summary(service: str, best, confidence: float) -> str:
    if best is None:
        return f"No conclusive cause found for the reported problem in {service}."
    qualifier = "likely" if confidence >= CONFIDENCE_FLOOR else "possible"
    return f"{qualifier.capitalize()} cause for the {service} incident: {best.statement}."


# ── Prompt ───────────────────────────────────────────────────────────────────


def render_findings(state: AgentState) -> str:
    """The evidence the analysis must be built from, and nothing else.

    References are shown next to each item because the model is asked to cite
    them; a citation it cannot spell is a citation that gets dropped.
    """
    lines = [
        f"Task: {state.get('task', '')}",
        f"Service: {state.get('target_service') or 'unknown'}",
        "",
        "Evidence (cite by reference, in brackets):",
    ]
    lines += [
        f"- [{item.reference}] ({item.kind}, via {item.source_tool}) {item.summary}"
        for item in state.get("evidence", [])
    ] or ["- (none)"]

    hypotheses = state.get("hypotheses", [])
    if hypotheses:
        lines += ["", "Correlations computed deterministically (treat as established fact):"]
        lines += [f"- ({h.confidence:.2f}) {h.statement}" for h in hypotheses]

    failures = [e for e in state.get("errors", []) if e.recoverable]
    if failures:
        lines += ["", "Data that could not be collected:"]
        lines += [f"- {e.message}" for e in failures]
    return "\n".join(lines)
