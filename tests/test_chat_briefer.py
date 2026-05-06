"""Tests for sponge.chat_briefer."""
from __future__ import annotations

from sponge.chat_briefer import _summarise_proposal, brief


def test_summarise_proposal_empty():
    out = _summarise_proposal({"nodes": [], "edges": []})
    assert "no graph mutations" in out


def test_summarise_proposal_with_nodes_and_edges():
    proposal = {
        "nodes": [{"id": "person_alice", "label": "Alice", "file_type": "person"}],
        "edges": [{"source": "person_alice", "target": "person_alice", "relation": "self"}],
    }
    out = _summarise_proposal(proposal)
    assert "Proposed 1 node" in out
    assert "Alice" in out
    assert "person_alice" in out


def test_brief_routes_through_provider(stub_provider):
    stub_provider.complete_text = "Got it — added Alice. Verify when you're ready."
    out = brief("met Alice today", {"nodes": [{"id": "person_alice", "label": "Alice", "file_type": "person"}], "edges": []})
    assert out == "Got it — added Alice. Verify when you're ready."

    # The system prompt is the briefer persona.
    sys_text = stub_provider.complete_calls[0]["system"]
    assert "briefer for a personal knowledge graph" in sys_text


def test_brief_returns_empty_string_on_provider_failure(monkeypatch):
    class BoomProvider:
        def complete(self, **kwargs):
            raise RuntimeError("api down")
    monkeypatch.setattr("sponge.chat_briefer.get_provider", lambda *a, **kw: BoomProvider())
    out = brief("hello", {"nodes": [], "edges": []})
    assert out == ""
