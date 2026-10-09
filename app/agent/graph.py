"""Сборка графа.

Форма рабочего процесса — это проектный документ. Чтение его должно отвечать
на вопрос «что этот агент умеет и чего не умеет», не читая промпт::

    START
      ↓
    analyze_task
      ↓
    collect_initial_context ──(нет сигнала)──→ insufficient_context ──→ END
      ↓
    correlate
      ↓
    select_tool ──(спрашивать больше нечего)───────────────┐
      ↓                                                    │
    execute_tool                                           │
      ↓                                                    ↓
    evaluate_observation ──(узнать больше)──→ select_tool  │
      └──(достаточно / бюджет исчерпан)───────────────────→ generate_analysis
                                                             ↓
                                                        propose_action
                                                             ↓
                        ┌──(нечего записывать)───────────────┤
                        ↓                                    ↓ (запись предложена)
                  final_response ←──(отклонено)──────── request_approval
                        ↑                               здесь граф встаёт на паузу
                        │                                    ↓ (подтверждено)
                        └──────────────────────────── execute_action
                        ↓
                       END

Три свойства выполняются по построению, а не по инструкции:

Детерминированная работа идёт первой. Сбор базовых данных и корреляция
выполняются до того, как к модели вообще обратятся, поэтому планировщик
рассуждает о фактах, а не решает, как их найти.

У каждого цикла есть выход, которым модель не управляет. Возврат к
select_tool защищён функцией маршрутизации, проверяющей бюджеты и прогресс;
планировщик может только сократить цикл, но никогда — продлить его.

Модель опциональна. Не передавайте чат-модель, и каждый узел всё равно
отработает, используя свою детерминированную замену. Именно это делает граф
тестируемым и превращает отказ провайдера в деградацию, а не в простой.

Ничто вне системы не меняется без человека. Единственная запись в графе
находится за request_approval, который прерывает исполнение. Пауза
надёжна — чекпоинтер сохраняет состояние — поэтому решение идёт отдельным
HTTP-запросом от отдельного человека, а не колбэком, удерживаемым в памяти.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.adapters.base import (
    CodeProvider,
    IssueProvider,
    KnowledgeProvider,
    LogProvider,
    MonitoringProvider,
)
from app.adapters.mock.providers import (
    MockCodeProvider,
    MockIssueProvider,
    MockLogProvider,
    MockMonitoringProvider,
)
from app.agent.guardrails import Guardrails
from app.agent.llm import build_chat_model
from app.agent.nodes.act import (
    make_execute_action_node,
    propose_action_node,
    request_approval_node,
    route_after_approval,
    route_after_proposal,
)
from app.agent.nodes.analyze_task import analyze_task_node
from app.agent.nodes.build_analysis import make_build_analysis_node
from app.agent.nodes.collect_context import make_collect_context_node
from app.agent.nodes.correlate import make_correlate_node
from app.agent.nodes.investigate import (
    evaluate_observation_node,
    make_execute_tool_node,
    make_route_after_evaluation,
    make_select_tool_node,
    route_after_selection,
)
from app.agent.planner import HeuristicPlanner, LLMPlanner, Planner
from app.agent.serde import agent_serializer
from app.agent.state import AgentState, ApprovalState, RunStatus
from app.agent.tools.catalog import build_registry
from app.services.cache import ToolCache


def run_config(run_id: str) -> dict[str, dict[str, str]]:
    """Чекпоинт-поток для одного запуска.

    Идентификатор запуска — это идентификатор потока, поэтому возобновление
    после подтверждения возобновляет то расследование и не может быть
    направлено на другое вызывающим, угадавшим иной идентификатор.
    """
    return {"configurable": {"thread_id": run_id}}


def has_enough_context(state: AgentState) -> Literal["correlate", "insufficient_context"]:
    """Шлюз между сбором и анализом.

    Написан как чистая функция состояния, чтобы маршрутизацию можно было
    юнит-тестировать без запуска графа — в этом основная ценность явного
    состояния.
    """
    if state.get("status") is RunStatus.FAILED:
        return "insufficient_context"
    context = state.get("context")
    if context is None or not context.metrics:
        return "insufficient_context"
    return "correlate"


async def insufficient_context_node(state: AgentState) -> AgentState:
    reasons = [e.message for e in state.get("errors", [])] or [
        "для запрошенного сервиса и окна не было данных мониторинга"
    ]
    return AgentState(
        current_step="insufficient_context",
        step_count=state.get("step_count", 0) + 1,
        status=RunStatus.FAILED,
        final_result=("Расследование остановилось до анализа: " + "; ".join(reasons) + "."),
    )


async def finalize_node(state: AgentState) -> AgentState:
    """Одно предложение о том, как завершился запуск, включая то, чего он не сделал.

    Запуск, предложивший действие и получивший отказ, должен сказать об этом:
    молчание читалось бы как «ничего не стоило делать», а это другой исход.
    """
    analysis = state.get("analysis")
    summary = analysis.summary if analysis else "Анализ не был построен."
    approval = state.get("approval_state")
    result = state.get("action_result") or {}

    if approval is ApprovalState.REJECTED:
        note = state.get("approval_note") or ""
        summary += " Предложенная задача не создана: проверяющий отклонил её."
        summary += f" Указанная причина: {note}" if note else ""
    elif approval is ApprovalState.APPROVED and result.get("ok"):
        issue = (result.get("issue") or {}).get("key", "задача")
        summary += f" Подтверждено и заведено как {issue}."
    elif approval is ApprovalState.APPROVED and not result.get("ok"):
        summary += " Подтверждённое действие не выполнилось; ничего не создано."

    return AgentState(
        current_step="final_response",
        step_count=state.get("step_count", 0) + 1,
        status=RunStatus.FAILED if state.get("status") is RunStatus.FAILED else RunStatus.COMPLETED,
        final_result=summary,
    )


def build_graph(
    *,
    monitoring: MonitoringProvider | None = None,
    code: CodeProvider | None = None,
    logs: LogProvider | None = None,
    issues: IssueProvider | None = None,
    knowledge: KnowledgeProvider | None = None,
    model: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    planner: Planner | None = None,
    guardrails: Guardrails | None = None,
    cache: ToolCache | None = None,
    use_llm: bool = True,
    enable_issue_tools: bool = True,
):
    """Скомпилировать рабочий процесс.

    Каждый участник инжектируем, потому что каждого из них тесту, сценарию
    оценки или деплою нужно подменять: mock- или MCP-провайдеры,
    заскриптованная или настоящая модель, более жёсткая политика.

    use_llm=False принудительно включает детерминированный путь, даже
    когда Ollama доступна — стенд оценки использует его как базу для
    сравнения с агентом, управляемым моделью.
    """
    monitoring = monitoring or MockMonitoringProvider()
    code = code or MockCodeProvider()
    logs = logs or MockLogProvider()
    if issues is None and enable_issue_tools:
        issues = MockIssueProvider()
    guardrails = guardrails or Guardrails()

    if use_llm and model is None:
        model = build_chat_model()
    if not use_llm:
        model = None
    planner = planner or (LLMPlanner(model) if model is not None else HeuristicPlanner())

    registry = build_registry(monitoring, code, logs, issues, knowledge)
    timeout = guardrails.tool_timeout_seconds

    builder = StateGraph(AgentState)
    builder.add_node("analyze_task", analyze_task_node)
    builder.add_node(
        "collect_initial_context",
        make_collect_context_node(monitoring, code, logs, timeout=timeout),
    )
    builder.add_node("correlate", make_correlate_node(code, timeout=timeout))
    builder.add_node("select_tool", make_select_tool_node(planner, registry, guardrails))
    builder.add_node("execute_tool", make_execute_tool_node(registry, guardrails, cache=cache))
    builder.add_node("evaluate_observation", evaluate_observation_node)
    builder.add_node("generate_analysis", make_build_analysis_node(model))
    builder.add_node("propose_action", propose_action_node)
    builder.add_node("request_approval", request_approval_node)
    builder.add_node("execute_action", make_execute_action_node(registry, guardrails))
    builder.add_node("insufficient_context", insufficient_context_node)
    builder.add_node("final_response", finalize_node)

    builder.add_edge(START, "analyze_task")
    builder.add_edge("analyze_task", "collect_initial_context")
    builder.add_conditional_edges(
        "collect_initial_context",
        has_enough_context,
        {"correlate": "correlate", "insufficient_context": "insufficient_context"},
    )
    builder.add_edge("correlate", "select_tool")
    builder.add_conditional_edges(
        "select_tool",
        route_after_selection,
        {"execute_tool": "execute_tool", "generate_analysis": "generate_analysis"},
    )
    builder.add_edge("execute_tool", "evaluate_observation")
    builder.add_conditional_edges(
        "evaluate_observation",
        make_route_after_evaluation(guardrails),
        {"select_tool": "select_tool", "generate_analysis": "generate_analysis"},
    )
    builder.add_edge("generate_analysis", "propose_action")
    builder.add_conditional_edges(
        "propose_action",
        route_after_proposal,
        {"request_approval": "request_approval", "final_response": "final_response"},
    )
    builder.add_conditional_edges(
        "request_approval",
        route_after_approval,
        {"execute_action": "execute_action", "final_response": "final_response"},
    )
    builder.add_edge("execute_action", "final_response")
    builder.add_edge("final_response", END)
    builder.add_edge("insufficient_context", END)

    # По умолчанию используется чекпоинтер в памяти, чтобы простой build_graph()
    # поддерживал паузу. Приложение подставляет постоянное хранилище, благодаря
    # которому ожидание подтверждения переживает перезапуск.
    return builder.compile(checkpointer=checkpointer or InMemorySaver(serde=agent_serializer()))
