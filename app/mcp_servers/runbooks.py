"""Safe loading of a small local Markdown runbook catalogue.

The directory is owned by the local product volume.  File names are never
accepted from an agent request, and symlinks/non-Markdown files are ignored,
so a runbook search cannot turn into arbitrary host file access.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from app.mcp_servers.knowledge import Document

MAX_RUNBOOK_BYTES = 1_000_000
_SERVICE = re.compile(r"^service:\s*([^#\r\n]+)\s*$", re.MULTILINE | re.IGNORECASE)
_TAGS = re.compile(r"^tags:\s*([^#\r\n]+)\s*$", re.MULTILINE | re.IGNORECASE)
_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def load_directory(directory: Path) -> tuple[Document, ...]:
    """Read direct child ``.md`` files into a deterministic, bounded corpus."""
    try:
        root = directory.resolve(strict=True)
    except FileNotFoundError:
        return ()
    if not root.is_dir():
        return ()
    documents: list[Document] = []
    for path in sorted(root.glob("*.md")):
        # ``glob`` may return a symlink. A local knowledge directory must not
        # be a back door to arbitrary paths on a Docker bind mount.
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RUNBOOK_BYTES:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not body.strip():
            continue
        title = _match(_TITLE, body) or path.stem.replace("-", " ")
        services = _csv(_match(_SERVICE, body))
        tags = _csv(_match(_TAGS, body))
        # Stable id avoids exposing the local path while still making a lookup
        # valid only for the document the search result referred to.
        document_id = "local-" + hashlib.sha256(path.name.encode()).hexdigest()[:16]
        documents.append(
            Document(
                doc_id=document_id,
                title=title[:200],
                body=body,
                services=services,
                tags=tags,
            )
        )
    return tuple(documents)


def _match(pattern: re.Pattern[str], value: str) -> str:
    match = pattern.search(value)
    return match.group(1).strip() if match else ""


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
