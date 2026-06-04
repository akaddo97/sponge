"""Tests for DefaultValidator.

The acceptance gate: the shipped synthetic demo graph must pass cleanly. If it
ever fails, a rule has leaked a domain-specific assumption into the generic
default. Everything else is one focused test per violation class on synthetic
fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sponge.validator import DefaultValidator, Validator, ValidationError

DEMO_GRAPH = (
    Path(__file__).resolve().parent.parent
    / "examples" / "demo_graph" / "graph.json"
)


def _graph(nodes, edges=None):
    return {"directed": True, "nodes": nodes, "edges": edges or []}


def test_default_validator_satisfies_protocol():
    assert isinstance(DefaultValidator(), Validator)


def test_demo_graph_passes():
    """Acceptance gate — the shipped demo graph is clean under the default."""
    graph = json.loads(DEMO_GRAPH.read_text())
    assert DefaultValidator().validate(graph) == []


def test_empty_graph_is_clean():
    assert DefaultValidator().validate(_graph([])) == []


def test_clean_minimal_graph():
    graph = _graph(
        [
            {"id": "a", "label": "A", "file_type": "person"},
            {"id": "b", "label": "B", "file_type": "company"},
        ],
        [{"source": "a", "target": "b", "relation": "works_at"}],
    )
    assert DefaultValidator().validate(graph) == []


def test_missing_node_field():
    graph = _graph([{"id": "a", "label": "A"}])  # no file_type
    violations = DefaultValidator().validate(graph)
    assert any("file_type" in v for v in violations)


def test_empty_string_node_field_rejected():
    graph = _graph([{"id": "a", "label": "  ", "file_type": "person"}])
    violations = DefaultValidator().validate(graph)
    assert any("label" in v for v in violations)


def test_duplicate_node_id():
    graph = _graph([
        {"id": "a", "label": "A", "file_type": "person"},
        {"id": "a", "label": "A again", "file_type": "person"},
    ])
    violations = DefaultValidator().validate(graph)
    assert any("duplicate" in v for v in violations)


def test_edge_missing_relation():
    graph = _graph(
        [{"id": "a", "label": "A", "file_type": "person"},
         {"id": "b", "label": "B", "file_type": "person"}],
        [{"source": "a", "target": "b"}],
    )
    violations = DefaultValidator().validate(graph)
    assert any("relation" in v for v in violations)


def test_edge_dangling_source():
    graph = _graph(
        [{"id": "b", "label": "B", "file_type": "person"}],
        [{"source": "ghost", "target": "b", "relation": "knows"}],
    )
    violations = DefaultValidator().validate(graph)
    assert any("ghost" in v and "does not exist" in v for v in violations)


def test_edge_dangling_target():
    graph = _graph(
        [{"id": "a", "label": "A", "file_type": "person"}],
        [{"source": "a", "target": "ghost", "relation": "knows"}],
    )
    violations = DefaultValidator().validate(graph)
    assert any("ghost" in v and "does not exist" in v for v in violations)


def test_embedded_edges_field_rejected():
    graph = _graph([
        {"id": "a", "label": "A", "file_type": "person",
         "edges": [{"source": "a", "target": "b"}]},
    ])
    violations = DefaultValidator().validate(graph)
    assert any("embedded" in v for v in violations)


def test_non_bool_verified_rejected():
    graph = _graph([
        {"id": "a", "label": "A", "file_type": "person", "verified": "yes"},
    ])
    violations = DefaultValidator().validate(graph)
    assert any("verified" in v for v in violations)


def test_links_key_accepted_as_edges():
    """A graph using the 'links' key is validated the same as 'edges'."""
    graph = {
        "directed": True,
        "nodes": [{"id": "a", "label": "A", "file_type": "person"}],
        "links": [{"source": "a", "target": "ghost", "relation": "knows"}],
    }
    violations = DefaultValidator().validate(graph)
    assert any("ghost" in v for v in violations)


def test_validation_error_carries_violations():
    err = ValidationError(["one", "two"])
    assert err.violations == ["one", "two"]
    assert "2 validation violation" in str(err)
