"""Conversation entity helpers — chat-graph style.

Conversations are graph nodes (file_type: conversation) whose messages live
in JSONL side-files at `data/conversations/<id>/messages.jsonl`. The graph
holds the conversation NODE (searchable, rooted, statistically aggregable);
the JSONL file holds the message body.

Caller is responsible for backend writes; this module manages node
construction + the JSONL side-files.
"""
from __future__ import annotations

import fcntl
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sponge._llm import default_provider_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def new_conversation_id(label: str | None = None) -> str:
    """Format: `conversation_<YYYY-MM-DD_HHMMSS>_<slug>_<short-uuid>`."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    short = uuid.uuid4().hex[:4]
    if label:
        slug = _slugify(label)
        if slug:
            return f"conversation_{ts}_{slug}_{short}"
    return f"conversation_{ts}_{short}"


def conversation_dir(base_dir: Path, conversation_id: str) -> Path:
    return Path(base_dir) / conversation_id


def messages_path(base_dir: Path, conversation_id: str) -> Path:
    return conversation_dir(base_dir, conversation_id) / "messages.jsonl"


def create_conversation_node(
    rooted_nodes: list[str],
    label: str,
    base_dir: Path,
    *,
    agent_provider: str | None = None,
    model: str | None = None,
    provisional_source: str | None = None,
    aliases: list[str] | None = None,
) -> dict:
    """Build a conversation node dict + initialise the messages JSONL file.

    Caller adds the returned dict to the GraphBackend. The messages file is
    touched empty so append_message can fcntl-lock from turn one.
    """
    if not rooted_nodes:
        raise ValueError("conversation must have at least one rooted_node")

    cid = new_conversation_id(label)
    now = _now_iso()

    cdir = conversation_dir(base_dir, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    messages_path(base_dir, cid).touch(exist_ok=False)

    node: dict = {
        "id": cid,
        "label": label,
        "file_type": "conversation",
        "rooted_nodes": list(rooted_nodes),
        "created_at": now,
        "updated_at": now,
        "agent_provider": agent_provider or default_provider_name(),
        "model": model,
        "message_count": 0,
        "aliases": list(aliases or []),
    }
    if provisional_source:
        node["provisional_source"] = provisional_source

    return node


def append_message(
    base_dir: Path,
    conversation_id: str,
    role: str,
    content: str,
    *,
    source: str = "user_typed",
    tool_calls: list | None = None,
    metadata: dict | None = None,
) -> dict:
    """Append a message to the JSONL under fcntl lock."""
    if role not in {"user", "assistant", "system"}:
        raise ValueError(f"role must be user|assistant|system, got {role!r}")

    msg = {"role": role, "content": content, "ts": _now_iso(), "source": source}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if metadata:
        msg["metadata"] = metadata

    path = messages_path(base_dir, conversation_id)
    if not path.exists():
        raise FileNotFoundError(
            f"messages file missing for {conversation_id} — "
            f"create_conversation_node must run before append_message"
        )

    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    return msg


def load_messages(
    base_dir: Path,
    conversation_id: str,
    limit: int | None = None,
) -> list[dict]:
    """Read messages from the conversation's JSONL. limit is a tail count."""
    path = messages_path(base_dir, conversation_id)
    if not path.exists():
        return []

    msgs: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                msgs.append(json.loads(line))

    if limit and limit > 0:
        return msgs[-limit:]
    return msgs


def conversations_for_node(nodes: list[dict], node_id: str) -> list[dict]:
    """All conversation nodes rooted at <node_id>, most recent first."""
    out = [
        n for n in nodes
        if n.get("file_type") == "conversation"
        and node_id in (n.get("rooted_nodes") or [])
    ]
    out.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    return out


def touch_conversation(node: dict, message_delta: int = 1) -> None:
    """Update updated_at + message_count on the node in-place."""
    node["updated_at"] = _now_iso()
    node["message_count"] = (node.get("message_count") or 0) + message_delta
