"""Tests for sponge.voice_pipeline.

Most assertions go via process_markdown_sidecar (no audio file required) so
the test suite stays Whisper-free.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sponge.voice_pipeline import process_markdown_sidecar


def _write_sidecar(tmp_path: Path, body: str) -> Path:
    md = tmp_path / "memo.md"
    md.write_text(
        "---\n"
        "captured_at: 2026-05-06T07:00:00Z\n"
        "transcript_engine: apple_on_device\n"
        "---\n\n"
        + body
    )
    return md


def test_pipeline_skips_proposer_when_disabled(stub_provider, backend, tmp_path):
    md = _write_sidecar(tmp_path, "I met Alice today.")
    result = process_markdown_sidecar(
        md, backend,
        skip_proposer=True,
        skip_briefer=True,
        cleaner_skip_llm=True,
        memo_dir=tmp_path / "out",
    )
    assert result.transcript == "I met Alice today."
    assert result.proposal == {"nodes": [], "edges": []}
    assert result.commit_summary["nodes_added"] == 0


def test_pipeline_runs_proposer_and_commits_provisional(stub_provider, backend, tmp_path):
    stub_provider.complete_text = json.dumps({
        "nodes": [{"id": "person_alice", "label": "Alice", "file_type": "person"}],
        "edges": [],
    })
    md = _write_sidecar(tmp_path, "I met Alice today at the cafe.")
    result = process_markdown_sidecar(
        md, backend,
        skip_briefer=True,
        cleaner_skip_llm=True,
        memo_dir=tmp_path / "out",
    )
    assert result.commit_summary["nodes_added"] == 1
    n = backend.get_node("person_alice")
    assert n is not None
    assert n["verified"] is False
    assert n["provisional_source"].startswith("voice_memo:")


def test_pipeline_writes_per_memo_trace(stub_provider, backend, tmp_path):
    stub_provider.complete_text = "{}"
    md = _write_sidecar(tmp_path, "Just a thought.")
    memo_dir = tmp_path / "out"
    process_markdown_sidecar(
        md, backend,
        skip_briefer=True,
        cleaner_skip_llm=True,
        memo_dir=memo_dir,
    )
    assert (memo_dir / "transcript.txt").exists()
    assert (memo_dir / "cleaned.txt").exists()
    assert (memo_dir / "result.json").exists()
    payload = json.loads((memo_dir / "result.json").read_text())
    assert "memo_id" in payload
    assert payload["captured_at"] == "2026-05-06T07:00:00Z"


def test_pipeline_briefer_runs_when_enabled(stub_provider, backend, tmp_path):
    stub_provider.complete_text = "Got it. Verify when you're ready."
    md = _write_sidecar(tmp_path, "I met Bob.")
    result = process_markdown_sidecar(
        md, backend,
        skip_proposer=True,  # short-circuit so the same provider serves both calls
        cleaner_skip_llm=True,
        memo_dir=tmp_path / "out",
    )
    assert "Verify when you're ready" in result.briefer_reply
