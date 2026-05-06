"""Audio transcription — `Transcriber` Protocol + reference impls.

Default: `WhisperLocalTranscriber` runs Whisper.cpp on the user's machine
via `pywhispercpp`. No audio leaves the laptop. Models live in
`models/ggml-*.bin`; install via `scripts/install_whisper.sh`.

Fallback: `MarkdownTranscriber` reads a sibling `.md` file the iOS Shortcut
can write directly (Apple's on-device speech-to-text does the heavy lifting,
the Shortcut writes a markdown frontmatter file). Useful when Whisper.cpp
isn't installed yet.

Selection: `get_transcriber()` reads the `TRANSCRIBER` env var. Default is
`whisper`; set to `markdown` to skip Whisper entirely.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> dict:
        """Transcribe an audio file. Returns:

            {
                "transcript": str,
                "engine": str,                     # e.g. "whisper_local"
                "duration_seconds": float | None,  # None if unknown
                "model": str | None,               # e.g. "ggml-base.en.bin"
            }
        """
        ...


# ── Whisper.cpp local ─────────────────────────────────────────────────────────


class WhisperLocalTranscriber:
    """Whisper.cpp via `pywhispercpp`. Runs locally — no audio leaves the
    machine. Model is loaded lazily on first call."""

    name = "whisper_local"

    def __init__(self, model_path: str | Path | None = None) -> None:
        # Default: Sponge looks for ggml-base.en.bin in models/ next to the
        # repo root. Override via constructor arg or WHISPER_MODEL env var.
        if model_path is None:
            model_path = os.environ.get(
                "WHISPER_MODEL",
                str(Path.cwd() / "models" / "ggml-base.en.bin"),
            )
        self.model_path = Path(model_path)
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            if not self.model_path.exists():
                raise RuntimeError(
                    f"Whisper model not found at {self.model_path}. "
                    "Run `bash scripts/install_whisper.sh` from the repo root, "
                    "or set WHISPER_MODEL to point at an existing ggml-base.en.bin."
                )
            from pywhispercpp.model import Model
            self._model = Model(str(self.model_path))
        return self._model

    def transcribe(self, audio_path: Path) -> dict:
        model = self._ensure_model()
        segments = model.transcribe(str(audio_path))
        text = " ".join(s.text.strip() for s in segments).strip()
        # pywhispercpp gives segment t0/t1 in 10ms units; last t1 ≈ duration
        duration = None
        if segments:
            try:
                duration = segments[-1].t1 / 100.0
            except AttributeError:
                pass
        return {
            "transcript": text,
            "engine": "whisper_local",
            "duration_seconds": duration,
            "model": self.model_path.name,
        }


# ── Markdown sidecar fallback ─────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_voice_memo_markdown(md_path: Path) -> tuple[dict, str]:
    """Parse a voice-memo markdown file with optional YAML frontmatter.

    Returns (frontmatter_dict, body_text). Frontmatter is parsed line-by-line
    as `key: value` pairs (no nested objects). Body is the text after the
    frontmatter block, or the whole file if no frontmatter.
    """
    raw = md_path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw.strip()
    fm_block, body = match.group(1), match.group(2)
    fm: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body.strip()


class MarkdownTranscriber:
    """Reads transcript text from a sidecar `.md` file. Used when an iOS
    Shortcut has done the speech-to-text on-device and dropped the result
    next to the audio file."""

    name = "markdown_sidecar"

    def transcribe(self, audio_path: Path) -> dict:
        sidecar = audio_path.with_suffix(".md")
        if not sidecar.exists():
            return {
                "transcript": "",
                "engine": "markdown_sidecar",
                "duration_seconds": None,
                "model": None,
            }
        _, body = parse_voice_memo_markdown(sidecar)
        return {
            "transcript": body,
            "engine": "markdown_sidecar",
            "duration_seconds": None,
            "model": None,
        }


# ── Factory ───────────────────────────────────────────────────────────────────


def get_transcriber(name: str | None = None, **kwargs) -> Transcriber:
    """Construct a Transcriber.

    Selection precedence: explicit `name` arg > TRANSCRIBER env var > whisper.
    """
    if name is None:
        name = os.environ.get("TRANSCRIBER", "whisper")
    if name in {"whisper", "whisper_local"}:
        return WhisperLocalTranscriber(**kwargs)
    if name in {"markdown", "markdown_sidecar"}:
        return MarkdownTranscriber()
    raise ValueError(f"unknown transcriber: {name!r}")
