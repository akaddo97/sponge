"""Tests for sponge.proposer."""
from __future__ import annotations

import json

from sponge.proposer import (
    _coerce_proposal,
    _strip_code_fence,
    commit_proposal,
    propose_from_transcript,
)


def test_strip_code_fence_handles_json_block():
    raw = "```json\n{\"a\": 1}\n```"
    assert _strip_code_fence(raw) == '{"a": 1}'


def test_strip_code_fence_handles_no_fence():
    assert _strip_code_fence('{"a": 1}') == '{"a": 1}'


def test_coerce_proposal_drops_invalid_node_types():
    payload = {
        "nodes": [
            {"id": "person_alice", "label": "Alice", "file_type": "person"},
            {"id": "thing_x", "label": "X", "file_type": "alien"},
        ],
        "edges": [],
    }
    out = _coerce_proposal(payload, existing_ids=set())
    assert len(out["nodes"]) == 1
    assert out["nodes"][0]["id"] == "person_alice"


def test_coerce_proposal_invents_id_when_missing():
    payload = {"nodes": [{"label": "Bob Smith", "file_type": "person"}], "edges": []}
    out = _coerce_proposal(payload, existing_ids=set())
    assert out["nodes"][0]["id"] == "person_bob_smith"


def test_coerce_proposal_drops_edges_pointing_to_unknown_nodes():
    payload = {
        "nodes": [{"id": "person_alice", "label": "Alice", "file_type": "person"}],
        "edges": [
            {"source": "person_alice", "target": "person_alice", "relation": "self"},
            {"source": "person_alice", "target": "person_zelda", "relation": "knows"},
        ],
    }
    out = _coerce_proposal(payload, existing_ids=set())
    # The bad edge's target isn't in the proposed nodes — drop it.
    assert len(out["edges"]) == 1


def test_propose_from_transcript_routes_through_provider(stub_provider, backend):
    stub_provider.complete_text = json.dumps({
        "nodes": [{"id": "person_alice", "label": "Alice", "file_type": "person"}],
        "edges": [],
    })
    result = propose_from_transcript("met Alice today", backend)
    assert result["nodes"][0]["id"] == "person_alice"
    # Provider was actually called.
    assert len(stub_provider.complete_calls) == 1
    sys_text = stub_provider.complete_calls[0]["system"]
    assert "graph mutations" in sys_text


def test_propose_from_transcript_handles_non_json_response(stub_provider, backend):
    stub_provider.complete_text = "not json at all"
    result = propose_from_transcript("ramble", backend)
    assert result == {"nodes": [], "edges": []}


def test_propose_from_transcript_strips_markdown_fence(stub_provider, backend):
    stub_provider.complete_text = (
        "```json\n"
        '{"nodes": [{"id": "person_x", "label": "X", "file_type": "person"}], "edges": []}\n'
        "```"
    )
    result = propose_from_transcript("met X", backend)
    assert result["nodes"][0]["id"] == "person_x"


def test_commit_proposal_writes_provisional_to_backend(backend):
    proposal = {
        "nodes": [{"id": "person_alice", "label": "Alice", "file_type": "person"}],
        "edges": [{"source": "person_alice", "target": "person_alice", "relation": "self"}],
    }
    summary = commit_proposal(proposal, source="voice_memo:abc", backend=backend)
    assert summary["nodes_added"] == 1
    assert summary["edges_added"] == 1

    n = backend.get_node("person_alice")
    assert n["verified"] is False
    assert n["provisional_source"] == "voice_memo:abc"
