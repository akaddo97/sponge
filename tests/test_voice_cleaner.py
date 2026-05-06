"""Tests for sponge.voice_cleaner."""
from __future__ import annotations

from sponge.voice_cleaner import (
    _apply_brand_caps,
    _dedup_stacked_fillers,
    _normalise_em_dashes,
    clean_transcript,
    load_spec,
)


def test_em_dash_normalisation():
    assert _normalise_em_dashes("a - b") == "a — b"
    assert _normalise_em_dashes("state-of-the-art") == "state-of-the-art"


def test_brand_caps_word_boundary():
    text, fired = _apply_brand_caps("i love openai", {"openai": "OpenAI"})
    assert text == "i love OpenAI"
    assert fired is True


def test_brand_caps_longest_match_wins():
    brands = {"new york": "New York", "new york city": "New York City"}
    text, _ = _apply_brand_caps("i went to new york city yesterday", brands)
    assert "New York City" in text


def test_stacked_fillers_collapse_only_when_repeated():
    text, fired = _dedup_stacked_fillers("you know, you know, this is real", ["you know"])
    assert text == "you know, this is real"
    assert fired is True

    text, fired = _dedup_stacked_fillers("you know this is real", ["you know"])
    assert "you know" in text  # single-instance filler preserved
    assert fired is False


def test_clean_transcript_skip_llm_returns_deterministic():
    out = clean_transcript("hello openai - world", skip_llm=True)
    assert out.text == "hello OpenAI — world"
    assert out.llm_used is False
    assert "brand_cap" in out.rules_applied
    assert "em_dash" in out.rules_applied


def test_clean_transcript_returns_diff():
    out = clean_transcript("openai is great", skip_llm=True)
    assert "openai" in out.diff
    assert "OpenAI" in out.diff


def test_clean_transcript_falls_back_on_llm_failure(monkeypatch, tmp_path):
    """When the LLM raises, the cleaner returns deterministic-only output."""
    class BoomProvider:
        def complete(self, **kwargs):
            raise RuntimeError("api down")

    monkeypatch.setattr("sponge.voice_cleaner.get_provider", lambda *a, **kw: BoomProvider())

    out = clean_transcript("hello openai world", log_path=tmp_path / "log.jsonl")
    assert out.text == "hello OpenAI world"
    assert out.llm_used is False


def test_clean_transcript_uses_llm_when_provider_returns(monkeypatch, tmp_path):
    class GoodProvider:
        def complete(self, **kwargs):
            return "polished output"

    monkeypatch.setattr("sponge.voice_cleaner.get_provider", lambda *a, **kw: GoodProvider())

    out = clean_transcript("messy input", log_path=tmp_path / "log.jsonl")
    assert out.text == "polished output"
    assert out.llm_used is True
    assert "llm_polish" in out.rules_applied


def test_load_spec_uses_defaults_when_path_missing(tmp_path):
    spec = load_spec(tmp_path / "missing.json")
    assert "openai" in spec["brands"]
    assert "you know" in spec["stacked_fillers"]


def test_load_spec_merges_user_overrides(tmp_path):
    import json
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"brands": {"foo": "Foo"}}))
    spec = load_spec(custom)
    assert spec["brands"] == {"foo": "Foo"}
    # heuristics fall through to defaults
    assert spec["heuristics"]["preserve_lol"] is True
