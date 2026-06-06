"""Commit guard — snapshot, apply, validate, roll back on failure.

The single chokepoint every graph-mutating commit should pass through. It
makes the validator's verdict binding: if the proposed commit would leave the
graph in a state the validator rejects, the store is returned to exactly where
it was and a `ValidationError` is raised. Nothing half-applied, nothing
corrupt.

The rollback is byte-identical for file backends because `snapshot()` captures
the raw bytes, not the parsed dict — re-serialising would silently reorder keys
and whitespace, which is "restored" only in spirit. A backend that can't
snapshot cheaply returns None from `snapshot()`; pass a `rollback` callable to
undo the mutation by hand in that case.
"""
from __future__ import annotations

from typing import Callable, TypeVar

from sponge.graph_backend import GraphBackend
from sponge.validator import Validator, ValidationError

T = TypeVar("T")


def guarded_commit(
    backend: GraphBackend,
    validator: Validator | None,
    apply: Callable[[], T],
    *,
    rollback: Callable[[], None] | None = None,
) -> T:
    """Run `apply()` (which mutates the backend), then validate.

    On a clean validation, return whatever `apply()` returned. On violations,
    restore the pre-commit state — `backend.restore(snapshot)` if the backend
    snapshots, else the supplied `rollback` callable — and raise
    `ValidationError` carrying the violations.

    A None validator disables the gate (apply runs unguarded). This keeps the
    seam optional: adopters who don't want validation pay nothing.
    """
    if validator is None:
        return apply()

    snapshot = backend.snapshot()
    result = apply()
    violations = validator.validate(backend.load_graph())
    if violations:
        if snapshot is not None:
            backend.restore(snapshot)
        elif rollback is not None:
            rollback()
        raise ValidationError(violations)
    return result
