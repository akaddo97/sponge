"""Tests for the commit gate: snapshot/restore, guarded_commit, and the
HTTP route behaviour (422 + byte-identical rollback).

The byte-identical assertion is the proof the rollback is real — not merely
"the graph parses to the same dict" but "the file on disk is the same bytes".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sponge.backends.json_file import JsonFileBackend
from sponge.commit import guarded_commit
from sponge.validator import DefaultValidator, ValidationError


# ── backend snapshot / restore round-trip ────────────────────────────────────

def test_snapshot_restore_round_trip(backend, graph_path):
    backend.add_node({"id": "a", "label": "A", "file_type": "person"})
    snap = backend.snapshot()
    before = graph_path.read_bytes()

    backend.add_node({"id": "b", "label": "B", "file_type": "person"})
    assert graph_path.read_bytes() != before  # mutation landed

    backend.restore(snap)
    assert graph_path.read_bytes() == before  # byte-identical


def test_snapshot_of_absent_file_restores_to_absent(tmp_path):
    path = tmp_path / "sub" / "graph.json"
    backend = JsonFileBackend(path)  # creates an empty file
    path.unlink()
    assert backend.snapshot() == b""  # absent file → empty token
    # Materialise a file, then restore the "absent" snapshot → file removed.
    backend._write({"directed": True, "nodes": [], "edges": []})
    assert path.exists()
    backend.restore(b"")
    assert not path.exists()


def test_load_graph_shape(backend):
    backend.add_node({"id": "a", "label": "A", "file_type": "person"})
    graph = backend.load_graph()
    assert [n["id"] for n in graph["nodes"]] == ["a"]
    assert graph["edges"] == []


# ── guarded_commit unit behaviour ────────────────────────────────────────────

def test_guarded_commit_clean_persists(backend):
    result = guarded_commit(
        backend,
        DefaultValidator(),
        lambda: backend.add_node({"id": "a", "label": "A", "file_type": "person"}),
    )
    assert result == "a"
    assert backend.get_node("a") is not None


def test_guarded_commit_rolls_back_on_violation(backend, graph_path):
    backend.add_node({"id": "a", "label": "A", "file_type": "person"})
    before = graph_path.read_bytes()

    # Apply an edge to a non-existent target — structurally invalid.
    def bad_apply():
        backend.add_edge({"source": "a", "target": "ghost", "relation": "knows"})

    with pytest.raises(ValidationError) as exc:
        guarded_commit(backend, DefaultValidator(), bad_apply)
    assert any("ghost" in v for v in exc.value.violations)
    assert graph_path.read_bytes() == before  # byte-identical rollback


def test_none_validator_disables_gate(backend):
    # No validator → the (invalid) mutation is allowed through unguarded.
    guarded_commit(
        None,  # backend unused when validator is None
        None,
        lambda: backend.add_edge({"source": "a", "target": "ghost", "relation": "x"}),
    )
    assert list(backend.all_edges())  # edge landed, no validation


# ── HTTP route: 422 + byte-identical on rejected commit ──────────────────────

@pytest.fixture
def app(backend):
    from sponge.app import create_app
    app = create_app(backend=backend)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_apply_clean_commit_persists(client, backend):
    backend.add_node(
        {"id": "person_a", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:ok",
    )
    resp = client.post("/api/verify/apply", json={"provisional_source": "voice_memo:ok"})
    assert resp.status_code == 200
    assert resp.get_json()["flipped"] == 1
    assert backend.get_node("person_a")["verified"] is True


def test_apply_rejected_commit_returns_422_and_byte_identical(client, backend, graph_path):
    # A provisional edge whose target never gets committed → after flipping the
    # edge to verified the graph still references a node that isn't there, so
    # the validator rejects the commit.
    backend.add_node({"id": "person_a", "label": "A", "file_type": "person"})
    backend.add_edge(
        {"source": "person_a", "target": "ghost", "relation": "knows"},
        provisional_source="voice_memo:bad",
    )
    before = graph_path.read_bytes()

    resp = client.post("/api/verify/apply", json={"provisional_source": "voice_memo:bad"})
    assert resp.status_code == 422
    body = resp.get_json()
    assert body["ok"] is False
    assert any("ghost" in v for v in body["violations"])
    # The store is left exactly as it was — nothing flipped, byte-identical.
    assert graph_path.read_bytes() == before


def test_apply_batch_rejected_commit_returns_422(client, backend, graph_path):
    backend.add_node({"id": "person_a", "label": "A", "file_type": "person"})
    backend.add_edge(
        {"source": "person_a", "target": "ghost", "relation": "knows"},
        provisional_source="voice_memo:bad",
    )
    before = graph_path.read_bytes()
    resp = client.post("/api/verify/apply_batch", json={"prefix": "voice_memo:"})
    assert resp.status_code == 422
    assert graph_path.read_bytes() == before


def test_custom_validator_injection(backend):
    from sponge.app import create_app

    class RejectEverything:
        def validate(self, graph):
            return ["nope"]

    app = create_app(backend=backend, validator=RejectEverything())
    app.config["TESTING"] = True
    backend.add_node(
        {"id": "n", "label": "N", "file_type": "person"},
        provisional_source="voice_memo:x",
    )
    resp = app.test_client().post(
        "/api/verify/apply", json={"provisional_source": "voice_memo:x"}
    )
    assert resp.status_code == 422
    assert resp.get_json()["violations"] == ["nope"]
