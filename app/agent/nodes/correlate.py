"""Скоррелировать собранные сигналы и забрать код за подозрительным релизом."""

from __future__ import annotations

from datetime import timedelta

from langgraph.graph._node import StateNode

from app.adapters.base import CodeProvider
from app.agent.correlation import (
    Spike,
    commits_in_release,
    deployments_before,
    detect_spike,
    rank_suspicious_commits,
)
from app.agent.state import AgentState, RunError
from app.agent.tooling import call_tool
from app.domain.models import Commit, Deployment, Evidence, EvidenceKind, Hypothesis

#: Насколько далеко до всплеска искать выпущенный вместе с ним код.
COMMIT_LOOKBACK = timedelta(hours=6)


def make_correlate_node(
    code: CodeProvider, *, timeout: float = 15.0
) -> StateNode[AgentState, None]:
    async def correlate_node(state: AgentState) -> AgentState:
        context = state.get("context")
        service = state.get("target_service") or "unknown"
        step = state.get("step_count", 0) + 1

        error_rate = context.metrics.get("error_rate") if context else None
        spike = detect_spike(error_rate) if error_rate else None

        # context отсутствует — значит, всплеск искать не в чём; проверка также сужает
        # тип context для mypy на всём дальнейшем пути.
        if context is None or spike is None:
            return AgentState(
                current_step="correlate",
                step_count=step,
                hypotheses=[],
                errors=[
                    RunError(
                        node="correlate",
                        kind="no_signal",
                        message="в запрошенном окне не найдено устойчивого всплеска частоты ошибок",
                    )
                ],
                observations=[{"node": "correlate", "spike": None}],
            )

        candidates = deployments_before(context.deployments, spike.started_at)
        evidence: list[Evidence] = [
            Evidence(
                kind=EvidenceKind.METRIC,
                summary=(
                    f"error_rate для {service} вырос с базовых {spike.baseline:.2%} "
                    f"до {spike.peak:.2%} ({spike.factor:.0f}x) начиная с "
                    f"{spike.started_at:%H:%M} UTC"
                ),
                source_tool="detect_spike",
                reference="error_rate",
                observed_at=spike.started_at,
            )
        ]

        records = []
        commits = []
        hypotheses: list[Hypothesis] = []

        if candidates:
            suspect = candidates[0]
            lead = spike.started_at - suspect.deployed_at
            outcome = await call_tool(
                "get_commits",
                lambda: code.get_commits(
                    service, suspect.deployed_at - COMMIT_LOOKBACK, suspect.deployed_at
                ),
                arguments={"service": service, "until": suspect.deployed_at.isoformat()},
                timeout=timeout,
                summarise=lambda c: f"{len(c)} commits",
            )
            records.append(outcome.record)
            all_commits = outcome.value or []
            commits = commits_in_release(all_commits, suspect) or all_commits[:1]

            stack_top = next((g.stack_top for g in context.error_groups if g.stack_top), None)
            ranked = rank_suspicious_commits(commits, stack_top)

            evidence.append(
                Evidence(
                    kind=EvidenceKind.DEPLOYMENT,
                    summary=(
                        f"{suspect.service} {suspect.version} развёрнут "
                        f"за {int(lead.total_seconds() // 60)} мин до всплеска"
                    ),
                    source_tool="get_recent_deployments",
                    reference=f"{suspect.service}@{suspect.version}",
                    observed_at=suspect.deployed_at,
                )
            )
            for commit in ranked[:3]:
                touched = ", ".join(f.path for f in commit.files) or "нет файлов"
                evidence.append(
                    Evidence(
                        kind=EvidenceKind.COMMIT,
                        summary=f"{commit.short_sha} {commit.message} (затрагивает {touched})",
                        source_tool="get_commits",
                        reference=commit.sha,
                        observed_at=commit.committed_at,
                    )
                )

            hypotheses.append(_release_hypothesis(suspect, ranked, spike, stack_top))
        else:
            hypotheses.append(
                Hypothesis(
                    statement=(
                        f"всплеск ошибок {service} в {spike.started_at:%H:%M} UTC "
                        "не объясняется ни одним деплоем в причинном окне"
                    ),
                    confidence=0.3,
                    supporting_evidence=("error_rate",),
                )
            )

        context.commits = commits
        return AgentState(
            current_step="correlate",
            step_count=step,
            tool_call_count=state.get("tool_call_count", 0) + len(records),
            tool_calls=records,
            evidence=evidence,
            hypotheses=hypotheses,
            context=context,
            observations=[
                {
                    "node": "correlate",
                    "spike_at": spike.started_at.isoformat(),
                    "spike_factor": round(spike.factor, 2),
                    "candidate_deployments": [d.version for d in candidates],
                }
            ],
        )

    return correlate_node


def _release_hypothesis(
    suspect: Deployment, ranked: list[Commit], spike: Spike, stack_top: str | None
) -> Hypothesis:
    """Уверенность растёт с силой совпадения и никогда не превышает 0.9.

    Временная близость сама по себе — корреляция; падающий стек-фрейм внутри
    файла, которого коснулся релиз, превращает её в защитимое утверждение. Даже
    тогда потолок остаётся ниже определённости — агент предлагает, человек
    решает.
    """
    confidence = 0.5
    supporting = ["error_rate", f"{suspect.service}@{suspect.version}"]

    lead_minutes = (spike.started_at - suspect.deployed_at).total_seconds() / 60
    if lead_minutes <= 10:
        confidence += 0.15

    top = ranked[0] if ranked else None
    if top and stack_top:
        module = stack_top.split(":")[0].strip()
        if any(module.endswith(f.path) for f in top.files):
            confidence += 0.25
            supporting.append(top.sha)

    statement = (
        f"{suspect.service} {suspect.version} привнёс сбой: он вышел "
        f"за {int(lead_minutes)} мин до роста частоты ошибок в {spike.factor:.0f}x"
    )
    if top:
        statement += f", и {top.short_sha} ({top.message}) меняет код на пути к сбою"

    return Hypothesis(
        statement=statement,
        confidence=min(confidence, 0.9),
        supporting_evidence=tuple(supporting),
    )
