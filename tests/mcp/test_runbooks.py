"""Local Markdown runbooks stay inside their configured catalogue."""

from __future__ import annotations

from app.mcp_servers.runbooks import MAX_RUNBOOK_BYTES, load_directory


def test_load_directory_extracts_metadata_and_does_not_expose_path(tmp_path):
    catalogue = tmp_path / "runbooks"
    catalogue.mkdir()
    (catalogue / "billing.md").write_text(
        "# Billing rollback\nservice: billing-service\ntags: rollback, incident\n\nRollback steps.",
        encoding="utf-8",
    )

    documents = load_directory(catalogue)

    assert len(documents) == 1
    assert documents[0].title == "Billing rollback"
    assert documents[0].services == ("billing-service",)
    assert documents[0].tags == ("rollback", "incident")
    assert str(catalogue) not in documents[0].doc_id


def test_load_directory_ignores_symlinks_non_markdown_and_oversized_files(tmp_path):
    catalogue = tmp_path / "runbooks"
    catalogue.mkdir()
    external = tmp_path / "outside.md"
    external.write_text("# Outside", encoding="utf-8")
    (catalogue / "linked.md").symlink_to(external)
    (catalogue / "not-a-runbook.txt").write_text("# Text", encoding="utf-8")
    (catalogue / "too-large.md").write_bytes(b"x" * (MAX_RUNBOOK_BYTES + 1))

    assert load_directory(catalogue) == ()


def test_missing_catalogue_is_empty(tmp_path):
    assert load_directory(tmp_path / "does-not-exist") == ()
