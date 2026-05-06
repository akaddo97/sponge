"""Two-pass voice memo cleaner.

Pass 1 (deterministic): brand capitalisation + em-dash normalisation +
stacked-filler dedup + spec-driven typo map. Always runs.

Pass 2 (LLM polish): paragraph breaks at topic shifts + light grammar +
sentence-boundary repair. Preserves voice character: "um", "uh", "like",
"you know", "lol" all survive.

The LLM polish is *never* allowed to block. Network errors, refusals,
missing API keys → return deterministic-only output. The pipeline contract
is "always returns a CleanedTranscript".
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sponge._llm import get_provider

log = logging.getLogger("sponge.voice_cleaner")

# Defensive char cap — at ~4 chars/token this maps to ~4k input tokens.
_MAX_INPUT_CHARS = 16_000


@dataclass
class CleanedTranscript:
    """`text` is the cleaned body; `diff` is a unified diff against the raw
    input; `rules_applied` lists rule kinds that fired (brand_cap,
    stacked_filler, em_dash, llm_polish); `llm_used` is True iff the polish
    pass ran successfully."""

    text: str
    diff: str
    rules_applied: list[str] = field(default_factory=list)
    llm_used: bool = False


# ── Spec ──────────────────────────────────────────────────────────────────────


_DEFAULT_SPEC: dict = {
    # Brand capitalisations applied to the raw transcript regardless of
    # how the user pronounced them. Override or extend via spec_path.
    "brands": {
        "openai": "OpenAI",
        "anthropic": "Anthropic",
        "github": "GitHub",
        "gitlab": "GitLab",
        "javascript": "JavaScript",
        "typescript": "TypeScript",
        "python": "Python",
        "macos": "macOS",
        "ios": "iOS",
        "iphone": "iPhone",
        "ipad": "iPad",
    },
    "stacked_fillers": ["you know", "like", "um", "uh"],
    "em_dash": True,
    "typos": {},
    "heuristics": {
        "preserve_lol": True,
        "preserve_um_unless_stacked": True,
        "max_paragraph_length_chars": 600,
    },
    "examples": [],
}


def _builtin_defaults() -> dict:
    # Deep-copy to defend against mutation by callers.
    return json.loads(json.dumps(_DEFAULT_SPEC))


def load_spec(spec_path: Path | None = None) -> dict:
    """Load a JSON spec file if given, else the built-in defaults. Missing
    keys fall through to defaults — partial specs are fine."""
    if spec_path is None or not Path(spec_path).exists():
        return _builtin_defaults()
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("voice cleaner spec %s unreadable (%s); using defaults", spec_path, exc)
        return _builtin_defaults()
    merged = _builtin_defaults()
    for k, v in spec.items():
        if k == "heuristics" and isinstance(v, dict):
            merged["heuristics"] = {**merged["heuristics"], **v}
        else:
            merged[k] = v
    return merged


# ── Deterministic pass ────────────────────────────────────────────────────────


def _normalise_em_dashes(text: str) -> str:
    """' - ' (space-hyphen-space) → ' — ' (em dash). Hyphens inside compound
    words (state-of-the-art) are left alone — they have no surrounding spaces."""
    return re.sub(r" - ", " — ", text)


def _apply_brand_caps(text: str, brands: dict[str, str]) -> tuple[str, bool]:
    """Replace case-insensitive matches of brand keys with the canonical-cased
    value. Word-boundary aware. Longer keys are tried first so 'New York City'
    wins over 'New York'."""
    fired = False
    keys = sorted(brands.keys(), key=len, reverse=True)
    for raw in keys:
        canonical = brands[raw]
        pattern = re.compile(rf"\b{re.escape(raw)}\b", re.IGNORECASE)
        new_text, n = pattern.subn(canonical, text)
        if n:
            fired = True
            text = new_text
    return text, fired


def _dedup_stacked_fillers(text: str, fillers: Iterable[str]) -> tuple[str, bool]:
    """Collapse 'you know, you know' → 'you know'. Only fires on **immediate**
    duplication separated by whitespace/comma — preserves single-instance
    fillers that carry voice character."""
    fired = False
    for filler in fillers:
        pattern = re.compile(
            rf"(\b{re.escape(filler)}\b)([,\s]+\b{re.escape(filler)}\b)+",
            re.IGNORECASE,
        )
        new_text, n = pattern.subn(r"\1", text)
        if n:
            fired = True
            text = new_text
    return text, fired


def _apply_typos(text: str, typos: dict[str, str]) -> tuple[str, bool]:
    fired = False
    for wrong, right in typos.items():
        pattern = re.compile(rf"\b{re.escape(wrong)}\b")
        new_text, n = pattern.subn(right, text)
        if n:
            fired = True
            text = new_text
    return text, fired


def _apply_deterministic(raw: str, spec: dict) -> tuple[str, list[str]]:
    """Pure function. Returns (cleaned_text, rule_kinds_that_fired)."""
    text = raw
    fired: list[str] = []

    if spec.get("em_dash", True):
        new = _normalise_em_dashes(text)
        if new != text:
            fired.append("em_dash")
            text = new

    text, brand_fired = _apply_brand_caps(text, spec.get("brands", {}))
    if brand_fired:
        fired.append("brand_cap")

    text, filler_fired = _dedup_stacked_fillers(text, spec.get("stacked_fillers", []))
    if filler_fired:
        fired.append("stacked_filler")

    text, typo_fired = _apply_typos(text, spec.get("typos", {}))
    if typo_fired:
        fired.append("typo")

    return text, fired


# ── LLM polish ────────────────────────────────────────────────────────────────


_POLISH_SYSTEM = (
    "You lightly polish voice memo transcripts. The user wants minimal "
    "cleanup — just enough to be readable on a screen.\n\n"
    "DO:\n"
    "- Add paragraph breaks at clear topic shifts (target paragraphs of "
    "≤600 characters).\n"
    "- Repair obvious sentence-boundary issues (a new sentence starting "
    "lowercase after a period).\n"
    "- Fix obvious typos that don't carry voice (e.g. 'ca't' → \"can't\").\n\n"
    "DO NOT:\n"
    "- Remove 'um', 'uh', 'like', 'you know', 'lol' — these carry voice.\n"
    "- Summarise, condense, or omit rambling content. Length stays close to "
    "the input.\n"
    "- Add facts, opinions, or framing that isn't already in the transcript.\n"
    "- Use markdown formatting. No asterisks for bold/italic, no headers, "
    "no bullet lists, no backticks. Plain prose only — output is rendered as "
    "text in a chat bubble, not parsed as markdown.\n\n"
    "Return ONLY the cleaned transcript. No preamble, no commentary."
)


def _build_few_shot(examples: list[dict]) -> list[dict]:
    msgs: list[dict] = []
    for ex in examples[:3]:
        raw = (ex.get("raw") or "").strip()
        cleaned = (ex.get("cleaned") or "").strip()
        if raw and cleaned:
            msgs.append({"role": "user", "content": raw})
            msgs.append({"role": "assistant", "content": cleaned})
    return msgs


def _apply_llm_polish(
    text: str,
    spec: dict,
    *,
    model: str | None = None,
) -> tuple[str, bool, str | None]:
    """Returns (polished_text, used_llm, error_reason).

    On any failure (no key, rate limit, refusal, network error) returns the
    input unchanged and a non-None error_reason. NEVER raises.
    """
    if len(text) > _MAX_INPUT_CHARS:
        return text, False, "input_too_large"

    examples = spec.get("examples") or []
    messages = _build_few_shot(examples) + [{"role": "user", "content": text}]

    try:
        kwargs: dict = {}
        if model is not None:
            kwargs["model"] = model
        provider = get_provider(**kwargs)
        polished = provider.complete(
            messages=messages,
            system=_POLISH_SYSTEM,
            max_tokens=4096,
        )
        if not polished:
            return text, False, "empty_response"
        return polished, True, None
    except Exception as exc:
        return text, False, f"{type(exc).__name__}: {str(exc)[:200]}"


# ── Logging ───────────────────────────────────────────────────────────────────


def _default_log_path() -> Path:
    return Path.cwd() / "logs" / "voice_cleaner.jsonl"


def _log_event(event: dict, log_path: Path | None = None) -> None:
    """Append a single JSON line. Best-effort — log failures are swallowed."""
    target = log_path or _default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── Public entry ──────────────────────────────────────────────────────────────


def clean_transcript(
    raw: str,
    spec: dict | None = None,
    *,
    skip_llm: bool = False,
    model: str | None = None,
    log_path: Path | None = None,
) -> CleanedTranscript:
    """Two-pass cleaner. Always returns a CleanedTranscript — never raises
    on LLM failure. `skip_llm=True` is the test/CI path."""
    spec = spec if spec is not None else load_spec()

    deterministic_text, det_rules = _apply_deterministic(raw, spec)

    final_text = deterministic_text
    rules = list(det_rules)
    llm_used = False
    llm_error: str | None = None

    if not skip_llm:
        polished, used, err = _apply_llm_polish(deterministic_text, spec, model=model)
        if used:
            final_text = polished
            llm_used = True
            rules.append("llm_polish")
        else:
            llm_error = err

    diff = "\n".join(difflib.unified_diff(
        raw.splitlines(),
        final_text.splitlines(),
        fromfile="raw",
        tofile="cleaned",
        lineterm="",
    ))

    _log_event(
        {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": "clean_transcript",
            "raw_chars": len(raw),
            "cleaned_chars": len(final_text),
            "rules_applied": rules,
            "llm_used": llm_used,
            "llm_error": llm_error,
        },
        log_path=log_path,
    )

    return CleanedTranscript(
        text=final_text,
        diff=diff,
        rules_applied=rules,
        llm_used=llm_used,
    )
