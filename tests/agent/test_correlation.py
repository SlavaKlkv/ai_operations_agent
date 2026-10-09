"""Корреляция — та часть, которую нельзя оставлять модели, поэтому она
tестируется как обычная арифметика: находит ли она ступеньку и отказывается
ли винить то, что случилось уже после факта."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.mock.dataset import BILLING_5XX, DEPLOY_AT, INCIDENT_START
from app.agent.correlation import (
    commits_in_release,
    deployments_before,
    detect_spike,
    rank_suspicious_commits,
)
from app.domain.models import ChangedFile, Commit, Deployment, MetricPoint, MetricSeries

BASE = datetime(2026, 3, 17, 14, 0, tzinfo=UTC)


def _series(values: list[float]) -> MetricSeries:
    return MetricSeries(
        service="svc",
        metric="error_rate",
        unit="ratio",
        points=tuple(
            MetricPoint(timestamp=BASE + timedelta(minutes=i), value=v)
            for i, v in enumerate(values)
        ),
    )


def test_detect_spike_finds_the_step_up():
    spike = detect_spike(_series([0.004] * 6 + [0.12] * 6))
    assert spike is not None
    assert spike.started_at == BASE + timedelta(minutes=6)
    assert spike.factor == pytest.approx(30.0, rel=0.01)


def test_flat_series_has_no_spike():
    assert detect_spike(_series([0.004] * 20)) is None


def test_single_blip_is_not_a_spike():
    """Один плохой отсчёт — шум; детектор требует, чтобы уровень держался."""
    assert detect_spike(_series([0.004] * 6 + [0.2] + [0.004] * 6)) is None


def test_large_relative_jump_below_absolute_floor_is_ignored():
    """0.00001 → 0.001 — стократный скачок, из-за которого никого не должны поднимать."""
    assert detect_spike(_series([0.00001] * 6 + [0.001] * 6)) is None


def test_real_scenario_spike_matches_the_dataset():
    spike = detect_spike(BILLING_5XX.metrics[("billing-service", "error_rate")])
    assert spike is not None
    assert spike.started_at == INCIDENT_START


def test_deployments_after_the_incident_are_not_candidates():
    later = Deployment(
        service="billing-service",
        version="v1.8.5",
        deployed_at=INCIDENT_START + timedelta(minutes=3),
        commit_sha="deadbeef",
    )
    earlier = Deployment(
        service="billing-service",
        version="v1.8.4",
        deployed_at=DEPLOY_AT,
        commit_sha="9f2c41ab",
    )
    assert deployments_before([later, earlier], INCIDENT_START) == [earlier]


def test_deployments_outside_the_causal_window_are_dropped():
    stale = Deployment(
        service="billing-service",
        version="v1.8.0",
        deployed_at=INCIDENT_START - timedelta(hours=5),
        commit_sha="0000",
    )
    assert deployments_before([stale], INCIDENT_START) == []


def test_commits_in_release_matches_by_sha():
    deployment = BILLING_5XX.deployments[0]
    released = commits_in_release(BILLING_5XX.commits, deployment)
    assert [c.sha for c in released] == [deployment.commit_sha]


def test_ranking_prefers_the_commit_touching_the_failing_file():
    touching = Commit(
        sha="a" * 40,
        message="feat: tax",
        author="x",
        committed_at=BASE,
        files=(ChangedFile(path="billing/charge.py", additions=60, deletions=4),),
    )
    unrelated = Commit(
        sha="b" * 40,
        message="chore: bump",
        author="y",
        committed_at=BASE,
        files=(ChangedFile(path="pyproject.toml", additions=1, deletions=1),),
    )
    ranked = rank_suspicious_commits(
        [unrelated, touching], "billing/charge.py:184 in apply_tax_rate"
    )
    assert ranked[0] is touching
