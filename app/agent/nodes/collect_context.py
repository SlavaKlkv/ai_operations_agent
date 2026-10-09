"""Получить базовый контекст, нужный каждому расследованию инцидента.

Этот узел намеренно не является точкой принятия решений: метрики, деплои,
группы ошибок и оповещения всегда стоит иметь, поэтому забор их через
круговой рейс к LLM добавил бы только задержку и недетерминизм. Выбор
инструментов начинается после этого узла, когда уже есть о чём рассуждать.
"""

from __future__ import annotations

from app.adapters.base import CodeProvider, LogProvider, MonitoringProvider
from app.agent.state import AgentState, CollectedContext, RunError, RunStatus
from app.agent.tooling import call_tool
from app.domain.models import (
    Alert,
    Deployment,
    ErrorGroup,
    Evidence,
    EvidenceKind,
    MetricSeries,
)

BASELINE_METRICS = ("error_rate", "latency_p99", "request_rate")


def _metric_evidence(series: MetricSeries) -> Evidence | None:
    peak = series.peak()
    if peak is None:
        return None
    return Evidence(
        kind=EvidenceKind.METRIC,
        summary=(
            f"{series.metric} для {series.service} достиг пика {peak.value} {series.unit} "
            f"(в среднем {series.mean():.4f} {series.unit} по окну)"
        ),
        source_tool="get_service_metrics",
        reference=f"{series.service}/{series.metric}",
        observed_at=peak.timestamp,
    )


def _deployment_evidence(deployment: Deployment) -> Evidence:
    return Evidence(
        kind=EvidenceKind.DEPLOYMENT,
        summary=(
            f"{deployment.service} {deployment.version} развёрнут в "
            f"{deployment.environment} из коммита {deployment.commit_sha[:8]}"
        ),
        source_tool="get_recent_deployments",
        reference=f"{deployment.service}@{deployment.version}",
        observed_at=deployment.deployed_at,
    )


def _error_evidence(group: ErrorGroup) -> Evidence:
    return Evidence(
        kind=EvidenceKind.LOG,
        summary=(
            f"{group.count}x {group.error_type} с "
            f"{group.first_seen:%H:%M} по {group.last_seen:%H:%M}"
            + (f" в {group.stack_top}" if group.stack_top else "")
        ),
        source_tool="get_error_groups",
        reference=group.error_type,
        observed_at=group.first_seen,
    )


def _alert_evidence(alert: Alert) -> Evidence:
    return Evidence(
        kind=EvidenceKind.ALERT,
        summary=f"алерт {alert.name} ({alert.severity}) сработал: {alert.description}",
        source_tool="get_recent_alerts",
        reference=alert.name,
        observed_at=alert.fired_at,
    )


def make_collect_context_node(
    monitoring: MonitoringProvider,
    code: CodeProvider,
    logs: LogProvider,
    *,
    timeout: float = 15.0,
):
    """Собрать узел, привязанный к конкретным провайдерам (mock, MCP-провайдеры, ...)."""

    async def collect_context_node(state: AgentState) -> AgentState:
        service = state.get("target_service")
        start, end = state.get("window_start"), state.get("window_end")
        if not service or start is None or end is None:
            return AgentState(
                current_step="collect_initial_context",
                step_count=state.get("step_count", 0) + 1,
                status=RunStatus.FAILED,
                errors=[
                    RunError(
                        node="collect_initial_context",
                        kind="insufficient_input",
                        message="из задачи не удалось определить целевой сервис или временное окно",
                        recoverable=False,
                    )
                ],
            )

        records = []
        context = CollectedContext()
        evidence: list[Evidence] = []
        observations: list[dict] = []
        errors: list[RunError] = []

        for metric in BASELINE_METRICS:
            outcome = await call_tool(
                "get_service_metrics",
                lambda m=metric: monitoring.get_service_metrics(service, m, start, end),
                arguments={"service": service, "metric": metric},
                timeout=timeout,
                summarise=lambda s: f"{len(s.points)} points",
            )
            records.append(outcome.record)
            if outcome.value is not None:
                context.metrics[metric] = outcome.value
                item = _metric_evidence(outcome.value)
                if item:
                    evidence.append(item)
                observations.append(
                    {
                        "tool": "get_service_metrics",
                        "metric": metric,
                        "peak": outcome.value.peak().value if outcome.value.peak() else None,
                        "mean": outcome.value.mean(),
                    }
                )
            elif outcome.record.error:
                errors.append(
                    RunError(
                        node="collect_initial_context",
                        kind="tool_failed",
                        message=f"get_service_metrics({metric}): {outcome.record.error}",
                    )
                )

        deploy_outcome = await call_tool(
            "get_recent_deployments",
            lambda: code.get_recent_deployments(service, start, end),
            arguments={"service": service},
            timeout=timeout,
            summarise=lambda d: f"{len(d)} deployments",
        )
        records.append(deploy_outcome.record)
        context.deployments = list(deploy_outcome.value or [])
        for deployment in deploy_outcome.value or []:
            evidence.append(_deployment_evidence(deployment))
            observations.append(
                {
                    "tool": "get_recent_deployments",
                    "version": deployment.version,
                    "deployed_at": deployment.deployed_at.isoformat(),
                    "commit_sha": deployment.commit_sha,
                }
            )

        log_outcome = await call_tool(
            "get_error_groups",
            lambda: logs.get_error_groups(service, start, end),
            arguments={"service": service},
            timeout=timeout,
            summarise=lambda g: f"{len(g)} error groups",
        )
        records.append(log_outcome.record)
        context.error_groups = list(log_outcome.value or [])
        for group in log_outcome.value or []:
            evidence.append(_error_evidence(group))
            observations.append(
                {
                    "tool": "get_error_groups",
                    "error_type": group.error_type,
                    "count": group.count,
                    "first_seen": group.first_seen.isoformat(),
                    "stack_top": group.stack_top,
                }
            )

        alert_outcome = await call_tool(
            "get_recent_alerts",
            lambda: monitoring.get_recent_alerts(service, start, end),
            arguments={"service": service},
            timeout=timeout,
            summarise=lambda a: f"{len(a)} alerts",
        )
        records.append(alert_outcome.record)
        context.alerts = list(alert_outcome.value or [])
        for alert in alert_outcome.value or []:
            evidence.append(_alert_evidence(alert))

        return AgentState(
            current_step="collect_initial_context",
            step_count=state.get("step_count", 0) + 1,
            tool_call_count=state.get("tool_call_count", 0) + len(records),
            tool_calls=records,
            evidence=evidence,
            context=context,
            observations=observations,
            errors=errors,
        )

    return collect_context_node
