"""GraphBackend — pluggable graph storage Protocol for Sponge.

Sponge is a voice-first frontend; the backend is yours. Implement this
Protocol against your storage of choice (JSON file, SQLite, Postgres,
Neo4j, your own). The reference implementation in
`sponge.backends.json_file.JsonFileBackend` is the simplest working version
— a single JSON file with atomic writes — and is what powers the demo.

The propose-approve-commit pattern is core to Sponge: voice memos produce
*provisional* nodes/edges with `verified=False`. The user reviews them on
the mobile verify pane and commits them (or rejects). The backend is
responsible for persisting that distinction.

Schema expectations (plain JSON):

    Node:  {"id": str, "label": str, "file_type": str, ...optional}
    Edge:  {"source": str, "target": str, "relation": str, ...optional}

Optional provisional metadata added by Sponge:

    "verified": bool                 — True once the user has approved
    "provisional_source": str        — e.g. "voice_memo:abc123" (free-form tag)
    "provisional_added_at": iso ts   — when Sponge first wrote it
"""
from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable


@runtime_checkable
class GraphBackend(Protocol):
    """Storage interface Sponge calls into.

    All methods are synchronous and expected to be cheap relative to LLM
    calls. Implementations should be thread-safe at the file/store level
    (Sponge uses a single Flask process today; future async work may grow
    this contract).
    """

    # --- node operations ---

    def add_node(self, node: dict, *, provisional_source: str | None = None) -> str:
        """Insert (or upsert by id) a node. If provisional_source is set,
        the node is marked verified=False and tagged with the source string.

        Returns the node id (caller may have built it; the backend may
        normalise / dedupe — return the canonical id used).
        """
        ...

    def get_node(self, node_id: str) -> dict | None:
        """Fetch one node. Return None if missing."""
        ...

    def update_node(self, node_id: str, fields: dict) -> bool:
        """Merge `fields` into the node. Return True if applied, False if
        the node didn't exist."""
        ...

    def all_nodes(self) -> Iterator[dict]:
        """Iterate every node (verified + provisional)."""
        ...

    # --- edge operations ---

    def add_edge(self, edge: dict, *, provisional_source: str | None = None) -> None:
        """Insert an edge. Like add_node, provisional_source flags it
        verified=False until committed."""
        ...

    def all_edges(self) -> Iterator[dict]:
        """Iterate every edge (verified + provisional)."""
        ...

    # --- provisional lifecycle ---

    def find_provisional(
        self,
        source: str | None = None,
        since_ts: float | None = None,
    ) -> dict:
        """Find provisional nodes and edges.

        Args:
            source: filter by provisional_source (exact match) — None = all
            since_ts: filter to entries added after this Unix timestamp

        Returns:
            {"nodes": [...], "edges": [...]} — both lists of dicts
        """
        ...

    def commit_provisional(self, source: str) -> int:
        """Mark all provisional entries with this source as verified=True.

        Returns the count of nodes+edges flipped.
        """
        ...

    def reject_provisional(self, source: str) -> int:
        """Delete all provisional entries with this source.

        Returns the count of nodes+edges removed.
        """
        ...

    # --- search (helpers used by the briefer + topic extraction) ---

    def find_nodes_by_label(self, query: str, limit: int = 10) -> list[dict]:
        """Case-insensitive partial-match label search. Most-recent first
        is a reasonable default but not required."""
        ...

    def stats(self) -> dict:
        """Cheap summary for the dashboard. Returns at least:
            {"node_count": int, "edge_count": int,
             "provisional_node_count": int, "provisional_edge_count": int}
        """
        ...

    # --- snapshot / restore (optional — powers the commit gate) ---

    def snapshot(self) -> bytes | None:
        """Return an opaque token capturing the store's exact current state,
        or None to opt out of snapshot-based rollback.

        Sponge's commit gate snapshots, applies a provisional commit, runs the
        validator, and — if the commit is rejected — calls `restore(token)` to
        return the store to its pre-commit state. For a file backend the token
        is the raw file bytes, so restore is byte-identical (re-serialising a
        parsed dict would reorder keys/whitespace). A backend that returns None
        signals it can't snapshot cheaply; the gate falls back to re-marking
        the just-committed entries provisional.
        """
        ...

    def restore(self, token: bytes) -> None:
        """Restore the store to the state captured by `snapshot()`."""
        ...

    def load_graph(self) -> dict:
        """Return the whole graph as a plain dict — at least
            {"nodes": [...], "edges": [...]}
        — for a Validator to inspect. Backend-agnostic: a database backend
        materialises the same shape from its tables."""
        ...
