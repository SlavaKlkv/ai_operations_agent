"""API-level tests: the HTTP contract and what actually lands in the database."""

from __future__ import annotations

import uuid

TASK = "После последнего релиза billing-service резко выросло количество 5xx. Разберись."
VAGUE = "что-то сломалось, непонятно где"


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["storage_backend"] == "sqlite"
    assert payload["cache_backend"] == "memory"
    assert payload["checkpointer"] == "memory"


async def test_a_run_that_wants_to_write_stops_and_says_what_it_wants(client):
    """The default outcome of a confident investigation is a pause, not a
    finished run: the agent has something to propose and no authority to do it."""
    response = await client.post("/runs", json={"task": TASK})
    assert response.status_code == 201

    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["target_service"] == "billing-service"
    assert body["analysis"]["service"] == "billing-service"
    assert "v1.8.4" in body["analysis"]["suspected_causes"][0]["statement"]
    assert body["analysis"]["requires_human_review"] is True

    pending = body["pending_approval"]
    assert pending["tool"] == "create_issue"
    assert "billing-service" in pending["arguments"]["title"]
    assert "## Evidence" in pending["arguments"]["body"], (
        "a reviewer approves content, so the content has to be in the response"
    )


async def test_tool_calls_are_persisted_for_audit(client):
    body = (await client.post("/runs", json={"task": TASK})).json()
    tools = [tc["tool"] for tc in body["tool_calls"]]
    assert "get_service_metrics" in tools
    assert "get_recent_deployments" in tools
    assert all(tc["ok"] for tc in body["tool_calls"])
    assert body["tool_call_count"] == len(body["tool_calls"])


async def test_run_can_be_fetched_again(client):
    created = (await client.post("/runs", json={"task": TASK})).json()
    fetched = (await client.get(f"/runs/{created['id']}")).json()
    assert fetched["id"] == created["id"]
    assert fetched["analysis"]["summary"] == created["analysis"]["summary"]
    assert fetched["pending_approval"]["tool"] == "create_issue"


async def test_listing_runs(client):
    await client.post("/runs", json={"task": TASK})
    await client.post("/runs", json={"task": VAGUE})
    runs = (await client.get("/runs")).json()
    assert len(runs) == 2
    assert {r["status"] for r in runs} == {"awaiting_approval", "failed"}


async def test_unknown_run_is_404(client):
    assert (await client.get(f"/runs/{uuid.uuid4()}")).status_code == 404


async def test_task_validation_rejects_junk(client):
    assert (await client.post("/runs", json={"task": "hi"})).status_code == 422
    assert (await client.post("/runs", json={"task": TASK, "nope": 1})).status_code == 422


async def test_failed_run_is_persisted_with_its_reason(client):
    body = (await client.post("/runs", json={"task": VAGUE})).json()
    assert body["status"] == "failed"
    assert body["analysis"] is None
    assert body["pending_approval"] is None
    assert "stopped before analysis" in body["final_result"]


# ── Approval ─────────────────────────────────────────────────────────────────


async def test_approving_resumes_the_run_and_creates_the_issue(client):
    created = (await client.post("/runs", json={"task": TASK})).json()
    response = await client.post(
        f"/runs/{created['id']}/approval",
        json={"approved": True, "note": "looks right"},
    )
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "completed"
    assert body["approved_by"] == "oncall@example.com"
    assert body["action_result"]["ok"] is True
    assert body["action_result"]["issue"]["key"].startswith("OPS-")
    assert "Approved and filed as OPS-" in body["final_result"]
    assert body["pending_approval"] is None, "the approval is no longer pending"


async def test_rejecting_ends_the_run_without_touching_anything(client):
    created = (await client.post("/runs", json={"task": TASK})).json()
    body = (
        await client.post(
            f"/runs/{created['id']}/approval",
            json={"approved": False, "note": "duplicate"},
        )
    ).json()

    assert body["status"] == "completed"
    assert body["action_result"] is None
    assert "was not created" in body["final_result"]
    assert "duplicate" in body["final_result"]
    assert "create_issue" not in [tc["tool"] for tc in body["tool_calls"]]


async def test_the_write_is_executed_exactly_once(client):
    """Approving twice must not file two issues."""
    created = (await client.post("/runs", json={"task": TASK})).json()
    decision = {"approved": True}

    first = await client.post(f"/runs/{created['id']}/approval", json=decision)
    second = await client.post(f"/runs/{created['id']}/approval", json=decision)

    assert first.status_code == 200
    assert second.status_code == 409
    assert "not awaiting approval" in second.json()["detail"]

    writes = [tc for tc in first.json()["tool_calls"] if tc["tool"] == "create_issue"]
    assert len(writes) == 1


async def test_a_decision_on_a_run_that_never_paused_is_refused(client):
    created = (await client.post("/runs", json={"task": VAGUE})).json()
    response = await client.post(
        f"/runs/{created['id']}/approval",
        json={"approved": True},
    )
    assert response.status_code == 409


async def test_a_decision_cannot_claim_to_be_someone_else(client):
    """Identity comes from the credential; a name in the body is a label."""
    created = (await client.post("/runs", json={"task": TASK})).json()
    response = await client.post(
        f"/runs/{created['id']}/approval",
        json={"approved": True, "decided_by": "someone.else@example.com"},
    )
    assert response.status_code == 422


async def test_a_decision_cannot_carry_its_own_action(client):
    """The content executed is what was checkpointed, so the request must not
    be able to smuggle different arguments past the reviewer."""
    created = (await client.post("/runs", json={"task": TASK})).json()
    response = await client.post(
        f"/runs/{created['id']}/approval",
        json={"approved": True, "arguments": {"title": "something else entirely"}},
    )
    assert response.status_code == 422


async def test_the_decision_is_recorded_before_the_action_runs(client, db_session):
    """ "Who approved this" has to be answerable even if the write then fails."""
    from sqlalchemy import select

    from app.db.models import Approval, AuditEvent

    created = (await client.post("/runs", json={"task": TASK})).json()
    await client.post(
        f"/runs/{created['id']}/approval",
        json={"approved": True, "note": "ship it"},
    )

    approvals = (await db_session.execute(select(Approval))).scalars().all()
    assert len(approvals) == 1
    assert approvals[0].state == "approved"
    assert approvals[0].decided_by == "oncall@example.com"
    assert approvals[0].decision_note == "ship it"
    assert approvals[0].decided_at is not None
    assert approvals[0].execution_result["ok"] is True

    actions = [e.action for e in (await db_session.execute(select(AuditEvent))).scalars()]
    assert actions == ["run.created", "run.awaiting_approval", "approval.approved", "run.finished"]


async def test_the_audit_trail_names_the_person_not_the_agent(client, db_session):
    from sqlalchemy import select

    from app.db.models import AuditEvent

    created = (await client.post("/runs", json={"task": TASK})).json()
    await client.post(
        f"/runs/{created['id']}/approval",
        json={"approved": False, "note": "not now"},
    )

    events = (await db_session.execute(select(AuditEvent))).scalars().all()
    decision = next(e for e in events if e.action == "approval.rejected")
    assert decision.actor == "oncall@example.com"
    assert decision.detail["note"] == "not now"
