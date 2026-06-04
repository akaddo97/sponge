"""Validator — pluggable structural gate for the commit boundary.

Sponge's third abstraction seam. The first two — `GraphBackend` (your store)
and the LLM provider (your model) — let you bring your own infrastructure.
This one lets you bring your own *rules*: what counts as a well-formed graph.

The propose-approve-commit loop is Sponge's whole point — the model proposes,
you commit, you stay the editor of your own graph. The validator makes that
promise enforceable: a commit that would corrupt the graph is rejected and the
on-disk graph is left byte-identical (see `sponge.commit`). Friction is the
feature; this is the guard rail behind it.

The shipped `DefaultValidator` carries only *generic structural* rules — the
invariants any graph store needs regardless of domain. It deliberately knows
nothing about your taxonomy. If you have a richer schema (required fields per
type, controlled relation vocabularies, status enums), implement `Validator`
yourself and inject it via `create_app(validator=...)` — exactly as you would
a custom backend.

    Node:  {"id": str, "label": str, "file_type": str, ...optional}
    Edge:  {"source": str, "target": str, "relation": str, ...optional}

`validate(graph)` returns a list of human-readable violation strings; an empty
list means clean. It never raises on graph content — malformed input is
reported as a violation, not an exception.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Validator(Protocol):
    """Structural gate Sponge calls before persisting a commit.

    A single method. Pure — no I/O, no mutation of the graph it inspects.
    Implementations receive the parsed graph dict (the backend's serialised
    form) and return every problem they find, so the user sees the full list
    in one pass rather than fixing one violation only to hit the next.
    """

    def validate(self, graph: dict) -> list[str]:
        """Return a list of violation strings. Empty list = clean."""
        ...


class DefaultValidator:
    """Generic structural rules — no domain schema, no taxonomy.

    Every rule here is one the synthetic demo graph satisfies. If a rule
    rejected the demo graph it would mean the rule had leaked someone's
    private schema into the shipped default; the demo graph passing is the
    standing acceptance test (see `tests/test_validator.py`).
    """

    def validate(self, graph: dict) -> list[str]:
        violations: list[str] = []

        nodes = graph.get("nodes", [])
        # Some serialisers emit "links" instead of "edges"; accept both, the
        # same way JsonFileBackend does on read.
        edges = graph.get("edges", graph.get("links", []))

        # ── 1. Every node has non-empty string id, label, file_type ──────────
        seen_ids: set[str] = set()
        for i, node in enumerate(nodes):
            nid = node.get("id")
            for field in ("id", "label", "file_type"):
                value = node.get(field)
                if not isinstance(value, str) or not value.strip():
                    ref = nid if isinstance(nid, str) and nid else f"index {i}"
                    violations.append(
                        f"NODE [{ref}]: '{field}' must be a non-empty string"
                    )

            # ── 2. Node ids are unique ───────────────────────────────────────
            if isinstance(nid, str) and nid:
                if nid in seen_ids:
                    violations.append(f"NODE [{nid}]: duplicate node id")
                seen_ids.add(nid)

            # ── 4. No node carries an embedded edges/links field ─────────────
            for embedded in ("edges", "links"):
                if embedded in node:
                    violations.append(
                        f"NODE [{nid or f'index {i}'}]: has embedded "
                        f"'{embedded}' field — edges belong only in the "
                        f"top-level edges array"
                    )

            # ── 5. If present, 'verified' must be a bool ─────────────────────
            if "verified" in node and not isinstance(node["verified"], bool):
                violations.append(
                    f"NODE [{nid or f'index {i}'}]: 'verified' must be a bool"
                )

        # ── 3. Every edge is well-formed and both endpoints resolve ──────────
        for i, edge in enumerate(edges):
            label = f"index {i}"
            endpoints_ok = True
            for field in ("source", "target", "relation"):
                value = edge.get(field)
                if not isinstance(value, str) or not value.strip():
                    violations.append(
                        f"EDGE [{label}]: '{field}' must be a non-empty string"
                    )
                    if field in ("source", "target"):
                        endpoints_ok = False

            if endpoints_ok:
                for field in ("source", "target"):
                    ref = edge[field]
                    if ref not in seen_ids:
                        violations.append(
                            f"EDGE [{label}] {edge.get('source')} "
                            f"--{edge.get('relation')}--> {edge.get('target')}: "
                            f"{field} '{ref}' does not exist as a node"
                        )

            if "verified" in edge and not isinstance(edge["verified"], bool):
                violations.append(
                    f"EDGE [{label}]: 'verified' must be a bool"
                )

        return violations


class ValidationError(Exception):
    """Raised by the commit guard when a proposed commit fails validation.

    Carries the full list of violations so the caller (an HTTP route, the
    voice pipeline) can surface them to the user.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(
            f"{len(violations)} validation violation(s): " + "; ".join(violations)
        )
