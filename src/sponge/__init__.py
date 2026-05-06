"""Sponge — voice-first agentic UI for personal knowledge graphs.

Tap to record on your phone → on-device transcribe → the model proposes graph
mutations → you verify before commit → conversational briefer replies. Self-
hosted. Provider-agnostic LLM. Bring your own graph.

Public surface:
    from sponge import GraphBackend, JsonFileBackend
    from sponge import voice_pipeline, voice_cleaner, transcription
    from sponge import chat_briefer, conversations
    from sponge.app import create_app
"""
from __future__ import annotations

from sponge.graph_backend import GraphBackend
from sponge.backends.json_file import JsonFileBackend

__all__ = [
    "GraphBackend",
    "JsonFileBackend",
    "__version__",
]

__version__ = "0.1.0"
