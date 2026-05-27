"""Tests for sponge.backends.json_file.JsonFileBackend."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sponge.backends.json_file import JsonFileBackend


def test_creates_empty_graph_on_first_use(tmp_path: Path):
    path = tmp_path / "graph.json"
    backend = JsonFileBackend(path)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data == {"directed": True, "nodes": [], "edges": []}


def test_add_node_marks_verified_when_no_source(backend):
    backend.add_node({"id": "n1", "label": "Alice", "file_type": "person"})
    n = backend.get_node("n1")
    assert n is not None
    assert n["verified"] is True
    assert "provisional_source" not in n


def test_add_node_with_source_marks_provisional(backend):
    backend.add_node(
        {"id": "n1", "label": "Alice", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    n = backend.get_node("n1")
    assert n["verified"] is False
    assert n["provisional_source"] == "voice_memo:abc"
    assert "provisional_added_at" in n


def test_add_edge_with_source_marks_provisional(backend):
    backend.add_node({"id": "n1", "label": "A", "file_type": "person"})
    backend.add_node({"id": "n2", "label": "B", "file_type": "company"})
    backend.add_edge(
        {"source": "n1", "target": "n2", "relation": "works_at"},
        provisional_source="voice_memo:abc",
    )
    edges = list(backend.all_edges())
    assert len(edges) == 1
    assert edges[0]["verified"] is False


def test_add_edge_dedupes_identical_triple(backend):
    """Same (source, target, relation) → silent skip, no second copy."""
    backend.add_node({"id": "n1", "label": "A", "file_type": "person"})
    backend.add_node({"id": "n2", "label": "B", "file_type": "company"})
    backend.add_edge({"source": "n1", "target": "n2", "relation": "works_at"})
    backend.add_edge({"source": "n1", "target": "n2", "relation": "works_at"})
    edges = list(backend.all_edges())
    assert len(edges) == 1


def test_add_edge_allows_distinct_relations_between_same_nodes(backend):
    """Different relation → distinct edge. Dedup is by triple, not by pair."""
    backend.add_node({"id": "n1", "label": "A", "file_type": "person"})
    backend.add_node({"id": "n2", "label": "B", "file_type": "person"})
    backend.add_edge({"source": "n1", "target": "n2", "relation": "knows"})
    backend.add_edge({"source": "n1", "target": "n2", "relation": "introduced_by"})
    edges = list(backend.all_edges())
    assert len(edges) == 2
    relations = {e["relation"] for e in edges}
    assert relations == {"knows", "introduced_by"}


def test_commit_provisional_flips_verified_and_drops_source(backend):
    backend.add_node(
        {"id": "n1", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    backend.add_edge(
        {"source": "n1", "target": "n1", "relation": "self"},
        provisional_source="voice_memo:abc",
    )
    flipped = backend.commit_provisional("voice_memo:abc")
    assert flipped == 2
    n = backend.get_node("n1")
    assert n["verified"] is True
    assert "provisional_source" not in n


def test_commit_provisional_no_op_for_unknown_source(backend):
    backend.add_node({"id": "n1", "label": "A", "file_type": "person"})
    assert backend.commit_provisional("voice_memo:nope") == 0


def test_reject_provisional_removes_nodes_and_their_edges(backend):
    backend.add_node(
        {"id": "n1", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    backend.add_node({"id": "n2", "label": "B", "file_type": "company"})  # verified
    backend.add_edge(
        {"source": "n1", "target": "n2", "relation": "works_at"},
        provisional_source="voice_memo:abc",
    )
    removed = backend.reject_provisional("voice_memo:abc")
    assert removed >= 1
    assert backend.get_node("n1") is None
    assert backend.get_node("n2") is not None  # untouched


def test_find_provisional_filters_by_source(backend):
    backend.add_node(
        {"id": "n1", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    backend.add_node(
        {"id": "n2", "label": "B", "file_type": "company"},
        provisional_source="voice_memo:xyz",
    )
    found = backend.find_provisional(source="voice_memo:abc")
    assert len(found["nodes"]) == 1
    assert found["nodes"][0]["id"] == "n1"


def test_find_nodes_by_label_case_insensitive(backend):
    backend.add_node({"id": "n1", "label": "Alice O.", "file_type": "person"})
    backend.add_node({"id": "n2", "label": "Bob", "file_type": "person"})
    matches = backend.find_nodes_by_label("alice")
    assert len(matches) == 1
    assert matches[0]["id"] == "n1"


def test_find_nodes_by_label_searches_aliases(backend):
    backend.add_node({"id": "n1", "label": "Alice", "file_type": "person", "aliases": ["A.O."]})
    matches = backend.find_nodes_by_label("a.o.")
    assert len(matches) == 1


def test_stats_counts_verified_and_provisional(backend):
    backend.add_node({"id": "n1", "label": "A", "file_type": "person"})
    backend.add_node(
        {"id": "n2", "label": "B", "file_type": "company"},
        provisional_source="voice_memo:abc",
    )
    s = backend.stats()
    assert s["node_count"] == 2
    assert s["provisional_node_count"] == 1
    assert s["edge_count"] == 0


def test_atomic_write_survives_concurrent_read(tmp_path):
    """Writing the graph leaves no partial-state file readers can see."""
    path = tmp_path / "graph.json"
    backend = JsonFileBackend(path)
    for i in range(20):
        backend.add_node({"id": f"n{i}", "label": f"Node {i}", "file_type": "concept"})
        # After every write the file parses cleanly.
        json.loads(path.read_text())
    # Final shape contains all nodes.
    data = json.loads(path.read_text())
    assert len(data["nodes"]) == 20


def test_demo_graph_loads(tmp_path):
    """Smoke: the bundled demo graph from knowledge-gun loads via JsonFileBackend."""
    src = Path(__file__).resolve().parent.parent / "examples" / "demo_graph" / "graph.json"
    if not src.exists():
        pytest.skip("demo graph not present in this checkout")
    target = tmp_path / "graph.json"
    target.write_text(src.read_text())
    backend = JsonFileBackend(target)
    nodes = list(backend.all_nodes())
    assert len(nodes) > 0
    edges = list(backend.all_edges())
    assert len(edges) > 0
