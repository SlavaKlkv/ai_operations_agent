"""Сам набор, запускаемый как интеграционный тест.

Это регрессионный барьер. Если изменение планировщика, арифметики
корреляции или анализа делает агента хуже хотя бы в одном из трёх
сценариев, это падает в CI, а не на демонстрации.
"""

from __future__ import annotations

import pytest

from app.evaluation.runner import render_report, run_suite
from app.evaluation.scenarios import SUITE, by_name


@pytest.fixture(scope="module")
async def suite_score():
    """Один прогон всего набора, общий для проверок ниже."""
    return await run_suite(use_llm=False)


async def test_the_deterministic_agent_passes_every_scenario(suite_score):
    assert suite_score.passed == suite_score.total, render_report(suite_score)


async def test_it_reaches_the_right_conclusion_on_a_release_incident():
    score = await run_suite([by_name("release-caused-incident")], use_llm=False)
    assert score.passed == 1, render_report(score)


async def test_it_refuses_to_blame_a_release_that_cannot_be_the_cause():
    """Тест с приманкой: деплой восьмичасовой давности не является причиной."""
    score = await run_suite([by_name("dependency-degradation")], use_llm=False)
    assert score.passed == 1, render_report(score)


async def test_it_reports_finding_nothing_rather_than_inventing_a_story():
    score = await run_suite([by_name("no-incident")], use_llm=False)
    assert score.passed == 1, render_report(score)


async def test_no_run_wastes_a_tool_call(suite_score):
    """Повторный или неудачный вызов — самый явный признак того, что агент барахтается."""
    assert suite_score.total_unnecessary_calls == 0, render_report(suite_score)


async def test_the_agent_stays_well_inside_its_budget(suite_score):
    assert suite_score.mean_tool_calls <= 10
    assert all(r.steps <= 30 for r in suite_score.runs)


async def test_every_scenario_is_covered_by_the_suite(suite_score):
    assert {r.scenario for r in suite_score.runs} == {s.name for s in SUITE}


async def test_the_report_names_what_failed():
    """Отчёт, который только считает успехи, бесполезен, когда что-то ломается."""
    broken = by_name("no-incident")
    impossible = type(broken)(
        **{
            **{f: getattr(broken, f) for f in broken.__slots__},
            "name": "impossible",
            "min_confidence": 0.99,
        }
    )
    score = await run_suite([impossible], use_llm=False)
    report = render_report(score)

    assert score.passed == 0
    assert "FAIL" in report
    assert "confidence_calibrated" in report
