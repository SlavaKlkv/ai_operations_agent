"""Typed access to the user-owned local Markdown runbook catalogue."""

from __future__ import annotations

from pathlib import Path

from app.domain.models import RunbookHit
from app.mcp_servers.knowledge import search_documents
from app.mcp_servers.runbooks import load_directory


class LocalRunbookProvider:
    """Reload the small catalogue for each run so edits take effect immediately."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def search_runbooks(
        self, query: str, service: str | None = None, limit: int = 3
    ) -> list[RunbookHit]:
        documents = load_directory(self.directory)
        if not documents:
            return []
        return [
            RunbookHit(
                doc_id=hit.doc_id,
                title=hit.title,
                excerpt=hit.excerpt,
                score=hit.score,
                services=tuple(hit.services),
                tags=tuple(hit.tags),
            )
            for hit in search_documents(documents, query, service, limit)
        ]
