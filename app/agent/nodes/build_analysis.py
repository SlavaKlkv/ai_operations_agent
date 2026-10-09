"""Собрать структурированный IncidentAnalysis из состояния.

Два пути дают одну схему. Без модели отчёт отрисовывается из доказательств
простым кодом, и каждое предложение отслеживаемо по построению. С моделью она
может улучшить формулировку наблюдаемых симптомов, но вердикт, уверенность,
рекомендованные действия и доказательства остаются детерминированными.

Это различие и есть вся стратегия привязки к доказательствам. У модели
запрашивают AnalysisDraft, который вообще не содержит поля
доказательств; доказательства, прикреплённые к готовому анализу, — это те
доказательства, которые фактически вернули инструменты. Модель не может
сослаться на метрику, которую ей никогда не показывали, потому что не она
пишет ссылки. Ссылки в её черновике всё равно проверяются до того, как
черновик будет использован, даже если итоговый вердикт приходит из
детерминированной корреляции.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph._node import StateNode
from pydantic import BaseModel, ConfigDict, Field

from app.agent.llm import LLMError, Usage, structured
from app.agent.state import AgentState, CollectedContext, RunError, RunStatus
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
- Write the observable symptoms in Russian.
- Be concise. No preamble, no restating the task.
"""


class AnalysisDraft(BaseModel):
    """То, что модели разрешено решать.

    Намеренно без поля evidence: ссылки прикрепляются из состояния и
    никогда не генерируются. См. docstring модуля.
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


def make_build_analysis_node(
    model: BaseChatModel | None = None,
) -> StateNode[AgentState, None]:
    """Собрать узел. Без модели он отрисовывает отчёт детерминированно."""

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


# ── Привязка к доказательствам ───────────────────────────────────────────────


def _drop_ungrounded(draft: AnalysisDraft, references: set[str]) -> AnalysisDraft:
    """Убрать ссылки, не соответствующие собранным доказательствам.

    Гипотеза, оставшаяся без подтверждающих доказательств, сохраняет своё
    утверждение, но теряет притязание на уверенность — она становится
    зацепкой, а не находкой. Молчаливое доверие выдуманной ссылке — именно тот
    сбой, ради предотвращения которого существует эта система.
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
    """Соединить формулировку модели с детерминированным вердиктом расследования.

    Модель может сделать наблюдаемые симптомы легче для чтения, но не должна
    превращать временную близость в причинность, повышать уверенность или
    делать слабую зацепку действенной. Эти решения уже есть в
    state.hypotheses и намеренно вычисляются простым кодом в узле
    корреляции.
    """
    hypotheses = list(state.get("hypotheses", []))
    deterministic = _deterministic(state, hypotheses)
    return deterministic.model_copy(update={"symptoms": draft.symptoms or deterministic.symptoms})


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


def _incident_start(state: AgentState) -> datetime | None:
    return next(
        (e.observed_at for e in state.get("evidence", []) if e.source_tool == "detect_spike"), None
    )


def _recommended_actions(context: CollectedContext | None, best: Hypothesis | None) -> list[str]:
    if best is None or best.confidence < CONFIDENCE_FLOOR:
        return [
            "Расширьте окно расследования и запустите его заново: текущих доказательств "
            "недостаточно, чтобы с достаточной уверенностью назвать причину."
        ]
    actions = []
    if context and context.deployments:
        suspect = context.deployments[0]
        actions.append(
            f"Откатите {suspect.service} до предыдущего релиза и подтвердите восстановление."
        )
    if context and context.commits:
        actions.append(
            f"Проверьте {context.commits[0].short_sha} — {context.commits[0].message} — "
            "на необработанный случай, видимый в логах."
        )
    actions.append("Добавьте регрессионный тест на падающий путь кода перед повторным деплоем.")
    return actions


def _summary(service: str, best: Hypothesis | None, confidence: float) -> str:
    if best is None:
        return f"Для проблемы в сервисе {service} убедительная причина не найдена."
    qualifier = "Вероятная" if confidence >= CONFIDENCE_FLOOR else "Возможная"
    return f"{qualifier} причина инцидента в {service}: {best.statement}."


# ── Промпт ───────────────────────────────────────────────────────────────────


def render_findings(state: AgentState) -> str:
    """Доказательства, из которых должен строиться анализ, и ничего больше.

    Ссылки показаны рядом с каждым пунктом, потому что модель просят
    ссылаться на них; ссылка, которую она не может воспроизвести, будет
    отброшена.
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
