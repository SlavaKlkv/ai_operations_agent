"""Детерминированные примитивы корреляции.

Определить, когда изменилась метрика и какой деплой ей предшествовал, —
это арифметика, а не понимание языка. Держать это на чистом Python значит, что
ответ воспроизводим, тестируем и не может быть выдуман: LLM позже объясняет
корреляцию, а не изобретает её.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.models import Commit, Deployment, MetricSeries

#: Изменение считается всплеском только при большом относительном и абсолютном
#: изменении, ради которого стоит отправить оповещение.
DEFAULT_RELATIVE_JUMP = 3.0
DEFAULT_ABSOLUTE_FLOOR = 0.01
#: Насколько ранний деплой ещё может считаться возможной причиной.
DEFAULT_CAUSAL_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Spike:
    started_at: datetime
    baseline: float
    peak: float

    @property
    def factor(self) -> float:
        return self.peak / self.baseline if self.baseline else float("inf")


def detect_spike(
    series: MetricSeries,
    *,
    relative_jump: float = DEFAULT_RELATIVE_JUMP,
    absolute_floor: float = DEFAULT_ABSOLUTE_FLOOR,
    baseline_points: int = 5,
) -> Spike | None:
    """Найти первый устойчивый скачок вверх в series.

    Базовый уровень — среднее первых baseline_points отсчётов, что
    предполагает, что окно начинается до инцидента — за выбор такого окна
    отвечает узел анализа задачи.
    """
    points = series.points
    if len(points) < baseline_points + 2:
        return None

    head = points[:baseline_points]
    baseline = sum(p.value for p in head) / len(head)
    if baseline <= 0:
        baseline = 1e-9

    for index, point in enumerate(points[baseline_points:], start=baseline_points):
        if point.value < absolute_floor or point.value / baseline < relative_jump:
            continue
        # Следующая точка тоже должна быть повышенной, чтобы игнорировать единичный всплеск.
        following = points[index + 1] if index + 1 < len(points) else point
        if following.value / baseline < relative_jump:
            continue
        peak = max(p.value for p in points[index:])
        return Spike(started_at=point.timestamp, baseline=baseline, peak=peak)
    return None


def deployments_before(
    deployments: list[Deployment],
    moment: datetime,
    *,
    window: timedelta = DEFAULT_CAUSAL_WINDOW,
) -> list[Deployment]:
    """Деплои, которые правдоподобно могли вызвать событие в moment.

    Упорядочены от ближайшего. Деплой после начала инцидента не может быть
    его причиной и отфильтровывается — именно это мешает агенту обвинить
    ложный релиз, случившийся через две минуты после начала инцидента.
    """
    candidates = [d for d in deployments if moment - window <= d.deployed_at <= moment]
    return sorted(candidates, key=lambda d: d.deployed_at, reverse=True)


def commits_in_release(commits: list[Commit], deployment: Deployment) -> list[Commit]:
    """Коммиты, отгруженные deployment: релизный коммит и всё до него.

    Синтетический провайдер возвращает плоскую историю, поэтому релизный
    коммит сопоставляется по SHA, а всё закоммиченное до него считается уже
    отгруженным. Настоящий VCS-провайдер ответил бы диапазоном ревизий.
    """
    released = next((c for c in commits if c.sha == deployment.commit_sha), None)
    if released is None:
        return []
    return [released]


def rank_suspicious_commits(commits: list[Commit], error_signature: str | None) -> list[Commit]:
    """Упорядочить коммиты по совпадению изменённых файлов с падающим стек-фреймом."""
    if not error_signature:
        return commits

    module = error_signature.split(":")[0].strip()

    def score(commit: Commit) -> tuple[int, int]:
        touched = sum(1 for f in commit.files if f.path and module.endswith(f.path))
        churn = sum(f.additions + f.deletions for f in commit.files)
        return (touched, churn)

    return sorted(commits, key=score, reverse=True)
