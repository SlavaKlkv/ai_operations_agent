"""Поиск по ранбукам видит только Markdown, явно положенный в каталог."""

from __future__ import annotations

import os
import sys

import pytest

from app.adapters.runbooks import LocalRunbookProvider
from app.mcp_servers.runbooks import load_directory


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


def test_runbook_loading_ignores_non_markdown_files(tmp_path):
    catalogue = tmp_path / "runbooks"
    catalogue.mkdir()
    (catalogue / "notes.txt").write_text("not a runbook", encoding="utf-8")
    (catalogue / "keep.md").write_text("# Keep\n\nrollback billing", encoding="utf-8")

    assert [document.title for document in load_directory(catalogue)] == ["Keep"]


def test_runbook_loading_is_not_recursive(tmp_path):
    catalogue = tmp_path / "runbooks"
    (catalogue / "nested").mkdir(parents=True)
    (catalogue / "nested" / "deep.md").write_text("# Deep\n\nhidden", encoding="utf-8")

    assert load_directory(catalogue) == ()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink needs elevated rights")
def test_runbook_loading_ignores_symbolic_links_to_outside_files(tmp_path):
    """Каталог знаний не должен быть чёрным ходом к файлам хоста."""
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret\n\nleaked", encoding="utf-8")
    catalogue = tmp_path / "runbooks"
    catalogue.mkdir()
    os.symlink(secret, catalogue / "link.md")

    assert load_directory(catalogue) == ()
