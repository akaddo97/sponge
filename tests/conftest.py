"""Shared fixtures.

`fake_provider` swaps `sponge._llm.get_provider` for a deterministic stub
so tests don't reach the network and don't need real API keys.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class FakeProvider:
    """Minimal Provider stub. Tests configure return values per-instance."""

    name = "fake"
    model = "fake-1"

    def __init__(self, complete_text: str = "", chat_chunks: list | None = None) -> None:
        self.complete_text = complete_text
        self.chat_chunks = chat_chunks or []
        self.complete_calls: list[dict] = []
        self.chat_calls: list[dict] = []

    def complete(self, messages, system="", max_tokens=1024, temperature=None):
        self.complete_calls.append({
            "messages": messages, "system": system,
            "max_tokens": max_tokens, "temperature": temperature,
        })
        return self.complete_text

    def chat(self, messages, system, tools=None, max_tokens=4096):
        self.chat_calls.append({
            "messages": messages, "system": system,
            "tools": tools, "max_tokens": max_tokens,
        })
        for chunk in self.chat_chunks:
            yield chunk


@pytest.fixture
def fake_provider():
    return FakeProvider()


@pytest.fixture
def graph_path(tmp_path: Path) -> Path:
    return tmp_path / "graph.json"


@pytest.fixture
def backend(graph_path):
    from sponge.backends.json_file import JsonFileBackend
    return JsonFileBackend(graph_path)


@pytest.fixture
def stub_provider(monkeypatch, fake_provider):
    """Patch sponge._llm.get_provider to return our FakeProvider."""
    monkeypatch.setattr("sponge._llm.get_provider", lambda *args, **kwargs: fake_provider)
    # Also patch the modules that imported it directly so they pick up the stub.
    for mod_path in (
        "sponge.voice_cleaner",
        "sponge.proposer",
        "sponge.chat_briefer",
    ):
        monkeypatch.setattr(f"{mod_path}.get_provider", lambda *args, **kwargs: fake_provider)
    return fake_provider
