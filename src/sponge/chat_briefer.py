"""Conversational briefer — single-turn reply summarising a voice memo's effect.

Used post-voice-memo: the user records a memo, Sponge transcribes it,
proposes graph changes, commits them as provisional. The briefer then
generates one short reply confirming what landed and gently nudging toward
verification.

Persona is deliberately neutral — a helpful note-taker, not a personality.
Adopters who want a sharper voice replace `BRIEFER_SYSTEM`.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sponge._llm import get_provider

log = logging.getLogger("sponge.chat_briefer")


BRIEFER_SYSTEM = (
    "You are a concise, helpful briefer for a personal knowledge graph.\n\n"
    "When the user records a voice memo, the system transcribes it and "
    "proposes graph mutations (new people, places, projects, concepts) "
    "based on what they said. Your job is to write a short conversational "
    "reply (1-3 sentences, plain prose, no bullets, no markdown) "
    "acknowledging what was captured and gently inviting verification.\n\n"
    "Style:\n"
    "- Address the user as if continuing a conversation. No \"Hello\" / "
    "\"Sure thing\" filler.\n"
    "- Reference at least one concrete detail from the proposal so the user "
    "knows you read it.\n"
    "- Close with a soft nudge to verify when they're ready (or skip the "
    "nudge if nothing graph-worthy landed).\n"
    "- Never invent facts. If the proposal is empty, just say nothing "
    "graph-worthy was captured this time.\n\n"
    "Examples of the tone (DO NOT repeat verbatim — match the register):\n"
    "  \"Got it — added Alice and her link to the cafe project. Verify when "
    "you're ready.\"\n"
    "  \"Captured the thinking on systematic strategies; nothing new for the "
    "graph yet — feels more like a private note.\"\n"
    "  \"Added a node for 'reservoir computing' and linked it to the AI "
    "concepts cluster. Take a look if you want to keep or edit.\""
)


def _summarise_proposal(proposal: dict) -> str:
    """Compact bullet-style summary the briefer can riff on. Stays close to
    the structure the LLM produced."""
    nodes = proposal.get("nodes") or []
    edges = proposal.get("edges") or []
    if not nodes and not edges:
        return "(no graph mutations proposed)"
    lines = [f"Proposed {len(nodes)} node(s), {len(edges)} edge(s)."]
    for n in nodes[:8]:
        lines.append(f"  - node {n['id']} ({n.get('file_type', '?')}): {n.get('label', '?')}")
    for e in edges[:8]:
        lines.append(f"  - edge {e['source']} —[{e.get('relation', '?')}]→ {e['target']}")
    return "\n".join(lines)


def brief(
    transcript: str,
    proposal: dict,
    *,
    max_tokens: int = 300,
    model: str | None = None,
) -> str:
    """Returns the briefer's reply text (plain prose, 1–3 sentences)."""
    summary = _summarise_proposal(proposal)
    user_payload = (
        f"User's voice memo (cleaned):\n{transcript}\n\n"
        f"What the system extracted:\n{summary}\n\n"
        f"Reply now."
    )
    kwargs: dict = {}
    if model is not None:
        kwargs["model"] = model
    provider = get_provider(**kwargs)
    try:
        text = provider.complete(
            messages=[{"role": "user", "content": user_payload}],
            system=BRIEFER_SYSTEM,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        log.warning("briefer failed: %s", exc)
        return ""
    return text.strip()
