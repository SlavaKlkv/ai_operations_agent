"""Deterministic correlation primitives.

Detecting *when* a metric changed and *which* deployment preceded it is
arithmetic, not language understanding. Keeping it in plain Python means the
answer is reproducible, testable, and cannot be hallucinated: the LLM later
explains the correlation, it does not invent it.
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
    """Find the first sustained step up in ``series``.

    The baseline is the mean of the first ``baseline_points`` samples, which
    assumes the window starts before the incident — the task analysis node is
    responsible for choosing such a window.
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
    """Deployments that could plausibly have caused an event at ``moment``.

    Ordered nearest-first. A deployment *after* the incident started cannot be
    its cause and is filtered out — this is what keeps the agent from blaming
    the decoy release that happened two minutes into the incident.
    """
    candidates = [d for d in deployments if moment - window <= d.deployed_at <= moment]
    return sorted(candidates, key=lambda d: d.deployed_at, reverse=True)


def commits_in_release(commits: list[Commit], deployment: Deployment) -> list[Commit]:
    """Commits shipped by ``deployment``: the release commit and anything before it.

    The synthetic provider returns a flat history, so the release commit is
    matched by SHA and everything committed before it is treated as already
    shipped. A real VCS provider would answer this with a revision range.
    """
    released = next((c for c in commits if c.sha == deployment.commit_sha), None)
    if released is None:
        return []
    return [released]


def rank_suspicious_commits(commits: list[Commit], error_signature: str | None) -> list[Commit]:
    """Order commits by how well their changed files match the failing stack frame."""
    if not error_signature:
        return commits

    module = error_signature.split(":")[0].strip()

    def score(commit: Commit) -> tuple[int, int]:
        touched = sum(1 for f in commit.files if f.path and module.endswith(f.path))
        churn = sum(f.additions + f.deletions for f in commit.files)
        return (touched, churn)

    return sorted(commits, key=score, reverse=True)
