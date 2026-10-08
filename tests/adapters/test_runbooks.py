"""Runbook search only sees Markdown explicitly placed in its catalogue."""

from __future__ import annotations

from app.adapters.runbooks import LocalRunbookProvider


async def test_local_runbook_provider_returns_typed_catalogue_hits(tmp_path):
    catalogue = tmp_path / "runbooks"
    catalogue.mkdir()
    (catalogue / "rollback.md").write_text(
        "# Billing rollback\n\nUse rollback when billing-service returns 5xx errors.",
        encoding="utf-8",
    )

    hits = await LocalRunbookProvider(catalogue).search_runbooks(
        "rollback 5xx", service="billing-service"
    )

    assert hits
    assert hits[0].doc_id.startswith("local-")
    assert hits[0].title == "Billing rollback"
    assert "5xx" in hits[0].excerpt


async def test_local_runbook_provider_ignores_empty_catalogue(tmp_path):
    assert await LocalRunbookProvider(tmp_path / "missing").search_runbooks("rollback") == []
