"""Аутентификация и разрешение, которое защищает запись.

Смысл этих тестов узкий и важный: имя, привязанное к подтверждению, должно
браться из учётных данных, а подтверждение должно требовать большего, чем
просто наличие таких данных.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.security import TOKEN_PREFIX, hash_token, issue_token
from app.db.models import AuditEvent, User

TASK = "После последнего релиза billing-service резко выросло количество 5xx. Разберись."


# ── Токены ───────────────────────────────────────────────────────────────────


def test_a_token_is_high_entropy_and_recognisable():
    token, digest = issue_token()
    assert token.startswith(TOKEN_PREFIX)
    assert len(token) > 40
    assert digest == hash_token(token)
    assert issue_token()[0] != token


async def test_the_token_itself_is_never_stored(db_session, approver_token):
    """Утёкшая база не должна отдавать рабочие учётные данные."""
    users = (await db_session.execute(select(User))).scalars().all()
    assert users
    for user in users:
        assert user.api_token_hash != approver_token
        assert len(user.api_token_hash) == 64


# ── Аутентификация ───────────────────────────────────────────────────────────


async def test_an_unauthenticated_request_is_refused(http_client, approver_token):
    response = await http_client.post("/runs", json={"task": TASK})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_an_unknown_token_is_refused(http_client, approver_token):
    http_client.headers["authorization"] = "Bearer aoa_not-a-real-token"
    assert (await http_client.post("/runs", json={"task": TASK})).status_code == 401


async def test_a_deactivated_user_cannot_authenticate(http_client, db_session, approver_token):
    user = (await db_session.execute(select(User))).scalars().one()
    user.is_active = False
    await db_session.commit()

    http_client.headers["authorization"] = f"Bearer {approver_token}"
    assert (await http_client.post("/runs", json={"task": TASK})).status_code == 401


async def test_a_valid_token_gets_through(client):
    assert (await client.post("/runs", json={"task": TASK})).status_code == 201


@pytest.mark.parametrize("path", ["/health", "/metrics"])
async def test_operational_endpoints_stay_open(http_client, path):
    """Проверка работоспособности, требующая учётных данных, — это проверка,
    которую настроят неверно; ни одна из точек не раскрывает содержимое запусков."""
    assert (await http_client.get(path)).status_code == 200


# ── Авторизация ──────────────────────────────────────────────────────────────


async def test_a_reader_can_investigate(reader_client):
    """Расследование только читает, поэтому разрешение на запись ему не нужно."""
    assert (await reader_client.post("/runs", json={"task": TASK})).status_code == 201


async def test_a_reader_cannot_approve_a_write(reader_client):
    created = (await reader_client.post("/runs", json={"task": TASK})).json()
    response = await reader_client.post(f"/runs/{created['id']}/approval", json={"approved": True})
    assert response.status_code == 403
    assert "may not approve" in response.json()["detail"]


async def test_a_refused_approval_changes_nothing(reader_client, db_session):
    created = (await reader_client.post("/runs", json={"task": TASK})).json()
    await reader_client.post(f"/runs/{created['id']}/approval", json={"approved": True})

    refetched = (await reader_client.get(f"/runs/{created['id']}")).json()
    assert refetched["status"] == "awaiting_approval", "the run still waits for someone who may"
    assert refetched["action_result"] is None
    assert "create_issue" not in [c["tool"] for c in refetched["tool_calls"]]


# ── Идентификатор пользователя в журнале аудита ──────────────────────────────


async def test_the_approver_recorded_is_the_authenticated_one(client, db_session):
    created = (await client.post("/runs", json={"task": TASK})).json()
    body = (await client.post(f"/runs/{created['id']}/approval", json={"approved": True})).json()

    assert body["approved_by"] == "oncall@example.com"
    events = (await db_session.execute(select(AuditEvent))).scalars().all()
    decision = next(e for e in events if e.action == "approval.approved")
    assert decision.actor == "oncall@example.com"


async def test_starting_a_run_is_attributed_to_the_caller(reader_client, db_session):
    await reader_client.post("/runs", json={"task": TASK})
    events = (await db_session.execute(select(AuditEvent))).scalars().all()
    created = next(e for e in events if e.action == "run.created")
    assert created.actor == "viewer@example.com"


async def test_authentication_can_be_switched_off_for_local_use(http_client, monkeypatch):
    """Сообщается через /health, чтобы деплой, оставивший её выключенной, это заметил."""
    from app.core.config import get_settings

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()

    assert (await http_client.post("/runs", json={"task": TASK})).status_code == 201
    assert (await http_client.get("/health")).json()["authentication"] is False

    get_settings.cache_clear()
