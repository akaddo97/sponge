"""Stub — illustrates the GraphBackend Protocol surface for a Postgres-backed BYOG.

Not production-ready. This file exists to make the README's "Bring your own graph"
diagram label `(yours)` concrete: it shows the shape of what you'd write to plug a
Postgres database into Sponge in place of the reference `JsonFileBackend`.

What's intentionally missing (do not copy-paste into production):

  - Connection pooling. A real impl uses ``psycopg.AsyncConnectionPool`` or
    SQLAlchemy's pool — opening a connection per call is not viable under load.
  - Migrations. The CREATE TABLE statements at the bottom are illustrative; in
    practice you'd manage them with Alembic / atlas / your tool of choice.
  - Transactions across multi-statement ops. ``commit_provisional`` flips a
    flag across two tables; a single transaction is required for atomicity.
  - Index strategy. The example assumes a trigram index on ``label`` for
    ``find_nodes_by_label`` — see the SQL at the bottom.
  - Error handling. Every method below would surface a meaningful exception
    for the caller; the stubs return defaults to keep the shape readable.

How to use this file:

  1. Copy it into your own repo (or a private branch of Sponge).
  2. Fill in the method bodies against your Postgres connection.
  3. Wire your backend into Sponge by setting the ``SPONGE_BACKEND`` env var
     or wherever you configure backend injection in your fork.

The Protocol the runtime checks against lives at ``sponge.graph_backend.GraphBackend``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator


class PostgresBackend:
    """Sketch of a Postgres-backed GraphBackend.

    Implements ``sponge.graph_backend.GraphBackend`` structurally. All methods
    raise ``NotImplementedError`` — fill them in before pointing Sponge at this.
    """

    def __init__(self, dsn: str) -> None:
        # In production: build the connection pool here.
        # e.g. ``self._pool = psycopg.AsyncConnectionPool(dsn, min_size=1, max_size=8)``
        self._dsn = dsn

    # --- node operations ---

    def add_node(self, node: dict, *, provisional_source: str | None = None) -> str:
        """INSERT ... ON CONFLICT (id) DO UPDATE — preserves the upsert-by-id
        semantics. Set ``verified=false`` and ``provisional_source`` /
        ``provisional_added_at`` when ``provisional_source`` is provided."""
        _ = (node, provisional_source)
        raise NotImplementedError

    def get_node(self, node_id: str) -> dict | None:
        """``SELECT * FROM nodes WHERE id = %s`` — return a dict (or None)."""
        _ = node_id
        raise NotImplementedError

    def update_node(self, node_id: str, fields: dict) -> bool:
        """``UPDATE nodes SET <fields> WHERE id = %s RETURNING 1`` — True if a
        row was affected, False otherwise."""
        _ = (node_id, fields)
        raise NotImplementedError

    def all_nodes(self) -> Iterator[dict]:
        """``SELECT * FROM nodes`` — stream with a server-side cursor on large
        graphs (``cursor.itersize``)."""
        raise NotImplementedError

    # --- edge operations ---

    def add_edge(self, edge: dict, *, provisional_source: str | None = None) -> None:
        """``INSERT ... ON CONFLICT (source, target, relation) DO NOTHING`` —
        matches the de-duplication contract of ``JsonFileBackend.add_edge``."""
        _ = (edge, provisional_source)
        raise NotImplementedError

    def all_edges(self) -> Iterator[dict]:
        """``SELECT * FROM edges`` — stream as above."""
        raise NotImplementedError

    # --- provisional lifecycle ---

    def find_provisional(
        self,
        source: str | None = None,
        since_ts: float | None = None,
    ) -> dict:
        """Two queries against the ``nodes`` and ``edges`` tables filtered on
        ``verified = false``. Return ``{"nodes": [...], "edges": [...]}``."""
        _ = (source, since_ts)
        raise NotImplementedError

    def commit_provisional(self, source: str) -> int:
        """Wrap the two updates (nodes + edges) in a single transaction so the
        verify-toggle is atomic across the pair. Return the rowcount sum."""
        _ = source
        raise NotImplementedError

    def reject_provisional(self, source: str) -> int:
        """Same transactional shape as commit_provisional — DELETE both tables
        where verified=false AND provisional_source=%s."""
        _ = source
        raise NotImplementedError

    # --- search ---

    def find_nodes_by_label(self, query: str, limit: int = 10) -> list[dict]:
        """ILIKE on ``label`` works for small graphs; for anything larger,
        wire up a pg_trgm GIN index and use the ``%`` operator."""
        _ = (query, limit)
        raise NotImplementedError

    def stats(self) -> dict:
        """Four cheap COUNT(*)s. On large graphs prefer ``pg_class.reltuples``
        as an approximation — the dashboard doesn't need exact precision."""
        raise NotImplementedError


# -----------------------------------------------------------------------------
# Reference schema. Treat this as a starting sketch, not a migration.
# -----------------------------------------------------------------------------
#
#   CREATE TABLE nodes (
#       id                    TEXT PRIMARY KEY,
#       label                 TEXT NOT NULL,
#       file_type             TEXT NOT NULL,
#       attrs                 JSONB NOT NULL DEFAULT '{}'::jsonb,
#       verified              BOOLEAN NOT NULL DEFAULT TRUE,
#       provisional_source    TEXT,
#       provisional_added_at  TIMESTAMPTZ
#   );
#
#   CREATE INDEX nodes_label_trgm ON nodes USING gin (label gin_trgm_ops);
#   CREATE INDEX nodes_provisional ON nodes (provisional_source)
#       WHERE verified = FALSE;
#
#   CREATE TABLE edges (
#       source                TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
#       target                TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
#       relation              TEXT NOT NULL,
#       attrs                 JSONB NOT NULL DEFAULT '{}'::jsonb,
#       verified              BOOLEAN NOT NULL DEFAULT TRUE,
#       provisional_source    TEXT,
#       provisional_added_at  TIMESTAMPTZ,
#       PRIMARY KEY (source, target, relation)
#   );
#
#   CREATE INDEX edges_provisional ON edges (provisional_source)
#       WHERE verified = FALSE;
#
# pg_trgm extension required for the label search index:
#
#   CREATE EXTENSION IF NOT EXISTS pg_trgm;


if __name__ == "__main__":  # pragma: no cover
    # Quick structural check: this class should satisfy the runtime Protocol.
    # Run with:  python examples/postgres_backend.py
    try:
        from sponge.graph_backend import GraphBackend  # type: ignore
    except ImportError:
        raise SystemExit(
            "Could not import sponge.graph_backend — install sponge into the active "
            "environment (uv pip install -e .) before running this check."
        )
    backend = PostgresBackend(dsn="postgresql://localhost/sponge_demo")
    assert isinstance(backend, GraphBackend), (
        "PostgresBackend does not structurally satisfy the GraphBackend Protocol."
    )
    print("OK — PostgresBackend matches the GraphBackend Protocol surface.")
    print(f"(method bodies still raise NotImplementedError as of {datetime.now(timezone.utc).date()}.)")
