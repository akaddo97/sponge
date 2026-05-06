"""Voice memo end-to-end pipeline.

1. Transcribe the audio (Whisper local by default; sidecar markdown fallback).
2. Clean the transcript (deterministic regex + LLM polish, both safe-fail).
3. Propose graph mutations (single LLM call returning JSON).
4. Commit the proposals as provisional via the GraphBackend (verified=False).
5. Generate a briefer reply summarising what landed.
6. Persist the per-memo trace under `data/voice_memos/<memo_id>/`.

The propose-approve-commit gate is non-negotiable: provisional entries are
visible on the verify pane. The user reviews and approves them — Sponge
never silently writes verified data from a voice memo.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sponge.chat_briefer import brief
from sponge.graph_backend import GraphBackend
from sponge.proposer import commit_proposal, propose_from_transcript
from sponge.transcription import (
    Transcriber,
    get_transcriber,
    parse_voice_memo_markdown,
)
from sponge.voice_cleaner import CleanedTranscript, clean_transcript


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_memo_id(stem: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "memo"
    if safe.startswith("voice_memo_"):
        return safe
    return f"voice_memo_{safe}"


@dataclass
class VoiceMemoResult:
    memo_id: str
    transcript: str
    cleaned: CleanedTranscript
    proposal: dict
    commit_summary: dict
    briefer_reply: str
    captured_at: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "memo_id": self.memo_id,
            "transcript": self.transcript,
            "cleaned_text": self.cleaned.text,
            "cleaned_diff": self.cleaned.diff,
            "rules_applied": self.cleaned.rules_applied,
            "llm_used": self.cleaned.llm_used,
            "proposal": self.proposal,
            "commit_summary": self.commit_summary,
            "briefer_reply": self.briefer_reply,
            "captured_at": self.captured_at,
            "metadata": self.metadata,
        }


def process_audio(
    audio_path: Path,
    backend: GraphBackend,
    *,
    memo_dir: Path | None = None,
    transcriber: Transcriber | None = None,
    skip_briefer: bool = False,
    skip_proposer: bool = False,
    cleaner_skip_llm: bool = False,
) -> VoiceMemoResult:
    """Run the full pipeline on a single audio file. Side files (transcript,
    proposal trace, briefer reply) are written under `memo_dir`; if not
    given, defaults to `<cwd>/data/voice_memos/<memo_id>/`.
    """
    audio_path = Path(audio_path)
    memo_id = _make_memo_id(audio_path.stem)

    # 1. Transcribe.
    tx = transcriber or get_transcriber()
    tx_result = tx.transcribe(audio_path)
    transcript = (tx_result.get("transcript") or "").strip()

    # 2. Clean.
    cleaned = clean_transcript(transcript, skip_llm=cleaner_skip_llm)

    # 3+4. Propose + commit (skippable for offline tests).
    proposal: dict = {"nodes": [], "edges": []}
    commit_summary: dict = {"nodes_added": 0, "edges_added": 0, "source": f"voice_memo:{memo_id}"}
    if not skip_proposer and cleaned.text:
        proposal = propose_from_transcript(cleaned.text, backend)
        commit_summary = commit_proposal(
            proposal,
            source=f"voice_memo:{memo_id}",
            backend=backend,
        )

    # 5. Briefer reply.
    reply = ""
    if not skip_briefer and cleaned.text:
        reply = brief(cleaned.text, proposal)

    result = VoiceMemoResult(
        memo_id=memo_id,
        transcript=transcript,
        cleaned=cleaned,
        proposal=proposal,
        commit_summary=commit_summary,
        briefer_reply=reply,
        captured_at=_now_iso(),
        metadata={k: v for k, v in tx_result.items() if k != "transcript"},
    )

    # 6. Persist trace.
    target_dir = Path(memo_dir) if memo_dir else Path.cwd() / "data" / "voice_memos" / memo_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
    (target_dir / "cleaned.txt").write_text(cleaned.text, encoding="utf-8")
    (target_dir / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if audio_path.exists() and audio_path.parent != target_dir:
        try:
            shutil.copy2(audio_path, target_dir / audio_path.name)
        except OSError:
            pass

    return result


def process_markdown_sidecar(
    md_path: Path,
    backend: GraphBackend,
    **kwargs,
) -> VoiceMemoResult:
    """Process a memo where transcription was already done by an iOS Shortcut
    (or similar) and dropped as a markdown sidecar. Reads the .md, runs the
    cleaner + proposer + briefer pipeline."""
    md_path = Path(md_path)
    fm, body = parse_voice_memo_markdown(md_path)
    audio_path = md_path.with_suffix(".m4a")
    memo_id = _make_memo_id(md_path.stem)

    cleaned = clean_transcript(body, skip_llm=kwargs.pop("cleaner_skip_llm", False))

    proposal: dict = {"nodes": [], "edges": []}
    commit_summary: dict = {"nodes_added": 0, "edges_added": 0, "source": f"voice_memo:{memo_id}"}
    if not kwargs.pop("skip_proposer", False) and cleaned.text:
        proposal = propose_from_transcript(cleaned.text, backend)
        commit_summary = commit_proposal(
            proposal,
            source=f"voice_memo:{memo_id}",
            backend=backend,
        )

    reply = ""
    if not kwargs.pop("skip_briefer", False) and cleaned.text:
        reply = brief(cleaned.text, proposal)

    result = VoiceMemoResult(
        memo_id=memo_id,
        transcript=body,
        cleaned=cleaned,
        proposal=proposal,
        commit_summary=commit_summary,
        briefer_reply=reply,
        captured_at=fm.get("captured_at", _now_iso()),
        metadata=fm,
    )

    memo_dir = kwargs.pop("memo_dir", None) or Path.cwd() / "data" / "voice_memos" / memo_id
    memo_dir = Path(memo_dir)
    memo_dir.mkdir(parents=True, exist_ok=True)
    (memo_dir / "transcript.txt").write_text(body, encoding="utf-8")
    (memo_dir / "cleaned.txt").write_text(cleaned.text, encoding="utf-8")
    (memo_dir / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if audio_path.exists():
        try:
            shutil.copy2(audio_path, memo_dir / audio_path.name)
        except OSError:
            pass

    return result
