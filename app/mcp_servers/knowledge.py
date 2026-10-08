"""Knowledge MCP server: runbooks and operational documentation.

Retrieval is one tool among several here, not the centre of the system. The
agent asks "is there a runbook for this" the same way it asks "what shipped" —
so the retrieval lives behind the same protocol as everything else, and the
ranking stays simple and inspectable rather than becoming a second project.

Scoring is BM25-flavoured lexical matching over a handful of documents: term
frequency, a length penalty, and a bonus for a service named in the metadata.
For a corpus of runbooks that is not a compromise — the vocabulary is small
and technical, and an engineer can predict what a query will return, which is
worth more here than a marginal gain in recall.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from app.mcp_servers.common import ToolFailure

MAX_EXCERPT_CHARS = 700
_WORD = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class Document:
    doc_id: str
    title: str
    body: str
    services: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


class SearchHit(BaseModel):
    doc_id: str
    title: str
    score: float
    services: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    excerpt: str


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    body: str
    services: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def search_documents(
    corpus: tuple[Document, ...], query: str, service: str | None = None, limit: int = 3
) -> list[SearchHit]:
    """Search a validated local catalogue without exposing file paths to callers."""
    terms = _tokenise(query)
    if not terms:
        raise ToolFailure("query must contain at least one searchable word")
    if not 1 <= limit <= 10:
        raise ToolFailure("limit must be between 1 and 10")
    average_length = sum(len(_tokenise(d.body)) for d in corpus) / max(len(corpus), 1)
    scored = [(_score(document, terms, service, average_length), document) for document in corpus]
    ranked = sorted(((s, d) for s, d in scored if s > 0), key=lambda pair: pair[0], reverse=True)
    return [
        SearchHit(
            doc_id=d.doc_id,
            title=d.title,
            score=s,
            services=list(d.services),
            tags=list(d.tags),
            excerpt=_excerpt(d, terms),
        )
        for s, d in ranked[:limit]
    ]


RUNBOOKS: tuple[Document, ...] = (
    Document(
        doc_id="rb-billing-rollback",
        title="Rolling back billing-service",
        services=("billing-service",),
        tags=("rollback", "release", "incident"),
        body=(
            "Roll back billing-service when a release causes elevated 5xx or charge "
            "failures.\n\n"
            "1. Identify the currently deployed version and the previous good one from "
            "the deployment history.\n"
            "2. Announce the rollback in #incidents with the incident timestamp.\n"
            "3. Re-deploy the previous tag. Deploys are immutable, so this is a forward "
            "deploy of an older artefact, not a state reversal.\n"
            "4. Invoice charges are idempotent by invoice id: re-processing a failed "
            "charge after rollback is safe and expected.\n"
            "5. Watch error_rate and latency_p99 for ten minutes. If the error rate does "
            "not return to its baseline within five minutes, the release is not the "
            "cause — escalate instead of rolling back further.\n"
        ),
    ),
    Document(
        doc_id="rb-tax-engine",
        title="Regional tax rate engine",
        services=("billing-service",),
        tags=("tax", "billing", "charge"),
        body=(
            "The tax engine resolves a regional rate for every invoice line before the "
            "total is charged.\n\n"
            "Rates are loaded from the rates table at start-up and cached for the process "
            "lifetime. A region with no configured rate resolves to None, and "
            "apply_tax_rate will raise a TypeError when it tries to multiply by it — this "
            "is the most common cause of unhandled errors in billing/charge.py.\n\n"
            "Mitigation: configure a fallback rate for the region, or deploy the guard "
            "that treats a missing rate as zero and reports it.\n"
        ),
    ),
    Document(
        doc_id="rb-gateway-timeouts",
        title="Payment gateway timeouts",
        services=("billing-service",),
        tags=("gateway", "timeout", "dependency"),
        body=(
            "GatewayTimeout errors mean the upstream payment provider did not respond "
            "within the configured budget.\n\n"
            "A low, steady rate of these is normal and is retried by the client. A sudden "
            "increase that is not accompanied by a change in request_rate points at the "
            "provider, not at us — check the provider status page before investigating "
            "our own releases.\n"
        ),
    ),
    Document(
        doc_id="rb-oncall-triage",
        title="On-call triage for elevated error rates",
        services=(),
        tags=("oncall", "triage", "process"),
        body=(
            "When error rate for a service rises above its alert threshold:\n\n"
            "1. Establish when it started, to the minute. Everything else depends on this.\n"
            "2. List deployments in the thirty minutes before that moment. A deploy after "
            "the start cannot be the cause.\n"
            "3. Aggregate the errors by type and read the top stack frame.\n"
            "4. Correlate: does a deployment in the window change the file in that frame? "
            "If yes, you have a candidate cause. If no, look at dependencies and "
            "infrastructure before blaming code.\n"
            "5. Mitigate before you diagnose fully. Rollback first, root-cause after.\n"
        ),
    ),
    Document(
        doc_id="rb-search-reindex",
        title="Reindexing search-service",
        services=("search-service",),
        tags=("search", "index", "maintenance"),
        body=(
            "Reindexing rebuilds the document index from the primary store. It is safe to "
            "run during traffic but roughly doubles read load, so avoid running it during "
            "an unrelated incident.\n"
        ),
    ),
)


def _tokenise(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _score(document: Document, terms: list[str], service: str | None, avg_len: float) -> float:
    """BM25-style term weighting, with a bonus for the right service.

    ``k1`` and ``b`` are the conventional defaults; there is no corpus here
    large enough to justify tuning them, and pretending otherwise would be
    false precision.
    """
    k1, b = 1.5, 0.75
    tokens = _tokenise(f"{document.title} {document.body} {' '.join(document.tags)}")
    if not tokens:
        return 0.0
    length_norm = 1 - b + b * (len(tokens) / avg_len)

    total = 0.0
    for term in terms:
        frequency = tokens.count(term)
        if frequency:
            total += (frequency * (k1 + 1)) / (frequency + k1 * length_norm)
    if total and service and service in document.services:
        total *= 1.5
    return round(total, 4)


def _excerpt(document: Document, terms: list[str]) -> str:
    """The passage around the first match, so a hit is judgeable without a read."""
    body = document.body
    lowered = body.casefold()
    position = min(
        (lowered.find(t) for t in terms if lowered.find(t) >= 0),
        default=0,
    )
    start = max(0, position - MAX_EXCERPT_CHARS // 3)
    snippet = body[start : start + MAX_EXCERPT_CHARS].strip()
    return ("…" if start else "") + snippet + ("…" if start + MAX_EXCERPT_CHARS < len(body) else "")


def build_server(corpus: tuple[Document, ...] = RUNBOOKS) -> MCPServer:
    server = MCPServer(
        name="ops-knowledge",
        version="1.0.0",
        instructions=(
            "Read-only operational documentation: runbooks, mitigation steps and "
            "service notes. Search first, then fetch a document by id when the "
            "excerpt suggests the full text is needed."
        ),
    )
    by_id = {d.doc_id: d for d in corpus}

    @server.tool(
        description=(
            "Search runbooks and operational notes by keywords, optionally biased "
            "towards one service. Returns ranked hits with an excerpt — use it to "
            "find documented mitigation before inventing one."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def search_runbooks(
        query: str, service: str | None = None, limit: int = 3
    ) -> list[SearchHit]:
        return search_documents(corpus, query, service, limit)

    @server.tool(
        description="Fetch one runbook in full by its document id.",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_runbook(doc_id: str) -> DocumentOut:
        document = by_id.get(doc_id)
        if document is None:
            raise ToolFailure(f"no runbook {doc_id!r}; known ids: {', '.join(sorted(by_id))}")
        return DocumentOut(
            doc_id=document.doc_id,
            title=document.title,
            body=document.body,
            services=list(document.services),
            tags=list(document.tags),
        )

    @server.resource(
        "knowledge://index",
        name="Runbook index",
        description="Every document this server holds, by id and title.",
        mime_type="application/json",
    )
    def index() -> list[dict[str, object]]:
        return [
            {"doc_id": d.doc_id, "title": d.title, "services": list(d.services)}
            for d in sorted(corpus, key=lambda d: d.doc_id)
        ]

    return server


def main() -> None:
    # Пустой каталог означает demo-режим. Когда пользователь добавил хотя бы
    # один корректный Markdown-файл, встроенные примеры не подмешиваются.
    from app.mcp_servers.runbooks import load_directory

    configured = os.getenv("RUNBOOKS_DIR", "").strip()
    corpus = load_directory(Path(configured)) if configured else ()
    build_server(corpus or RUNBOOKS).run("stdio")


if __name__ == "__main__":
    main()
