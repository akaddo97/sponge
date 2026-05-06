"""Voice memo → graph proposal.

Takes a cleaned transcript + a snapshot of the existing graph, asks the
configured LLM provider to propose graph mutations, returns a structured
proposal dict the caller can hand to the GraphBackend.

This is intentionally simpler than a multi-round tool-using agent: ONE
sync LLM call returning JSON. That keeps Sponge v0.1 understandable —
a 200-line module with a single LLM round-trip — and aligns with the
propose-approve-commit gate where the user is the final reviewer anyway.

Adopters who want richer proposal logic (multi-round agentic, tool-calling,
external search) replace this module wholesale; the rest of Sponge doesn't
care how proposals are shaped, only that they conform to the JSON schema.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from sponge._llm import get_provider
from sponge.graph_backend import GraphBackend

log = logging.getLogger("sponge.proposer")


PROPOSER_SYSTEM = (
    "You extract structured graph mutations from conversational voice memos.\n\n"
    "Given a cleaned transcript, you return a JSON object with two keys: "
    "`nodes` (new entities mentioned) and `edges` (relationships between "
    "entities). Each node has `id`, `label`, `file_type` (one of: person, "
    "company, institution, project, concept). Each edge has `source`, "
    "`target`, `relation` (a short verb phrase like 'works_at', 'mentioned', "
    "'founded'). When the transcript references an entity already in the "
    "graph (provided as context below), reuse its existing id rather than "
    "minting a new one.\n\n"
    "Be conservative: only propose what's explicitly in the transcript. The "
    "user reviews every proposal before commit, so accuracy beats coverage. "
    "If the transcript contains nothing graph-worthy (e.g. a fleeting "
    "thought), return empty arrays.\n\n"
    "Output ONLY the JSON object. No preamble, no markdown fence, no comments."
)


_NODE_FILE_TYPES = {"person", "company", "institution", "project", "concept", "topic"}
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _build_context(backend: GraphBackend, max_nodes: int = 200) -> str:
    """Compact list of existing nodes for the LLM to dedupe against. Keeps
    only label + id + type — light enough to fit in a system prompt for
    graphs up to ~200 nodes. Larger graphs need a search-then-propose flow."""
    rows: list[str] = []
    for node in backend.all_nodes():
        if len(rows) >= max_nodes:
            break
        nid = node.get("id", "?")
        label = node.get("label", nid)
        ft = node.get("file_type", "?")
        rows.append(f"  {nid}  ({ft}) — {label}")
    if not rows:
        return "(graph is empty — propose new nodes freely)"
    return "Existing graph entities (reuse these ids when appropriate):\n" + "\n".join(rows)


def _slugify_id(label: str, file_type: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not base:
        base = "node"
    return f"{file_type}_{base}"


def _coerce_proposal(payload: dict, existing_ids: set[str]) -> dict:
    """Sanitise the LLM's JSON output: drop malformed entries, normalise
    ids, default missing fields where safe."""
    out_nodes: list[dict] = []
    out_edges: list[dict] = []
    seen_ids: set[str] = set(existing_ids)

    for raw in payload.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        label = (raw.get("label") or "").strip()
        ft = (raw.get("file_type") or "").strip().lower()
        if not label or ft not in _NODE_FILE_TYPES:
            continue
        nid = (raw.get("id") or "").strip().lower()
        if not nid or not _ID_RE.match(nid):
            nid = _slugify_id(label, ft)
        # Avoid colliding with an existing id while still "new".
        candidate = nid
        suffix = 2
        while candidate in seen_ids and candidate not in existing_ids:
            candidate = f"{nid}_{suffix}"
            suffix += 1
        seen_ids.add(candidate)
        out_nodes.append({"id": candidate, "label": label, "file_type": ft})

    valid_ids = seen_ids
    for raw in payload.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        src = (raw.get("source") or "").strip()
        tgt = (raw.get("target") or "").strip()
        rel = (raw.get("relation") or "").strip().lower().replace(" ", "_")
        if not src or not tgt or not rel:
            continue
        if src not in valid_ids or tgt not in valid_ids:
            continue
        out_edges.append({"source": src, "target": tgt, "relation": rel})

    return {"nodes": out_nodes, "edges": out_edges}


def _strip_code_fence(text: str) -> str:
    """Remove a leading ```json fence and trailing ``` if present."""
    text = text.strip()
    if text.startswith("```"):
        # Drop the first line (```json or ```) and the closing fence.
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def propose_from_transcript(
    transcript: str,
    backend: GraphBackend,
    *,
    max_tokens: int = 2048,
    model: str | None = None,
) -> dict:
    """Single LLM call. Returns `{"nodes": [...], "edges": [...]}`.

    The LLM may also include a `reasoning` field describing why it
    proposed what it proposed; the caller is welcome to surface that
    to the user. We don't enforce its presence.
    """
    context = _build_context(backend)
    system = f"{PROPOSER_SYSTEM}\n\n{context}"

    kwargs: dict = {}
    if model is not None:
        kwargs["model"] = model
    provider = get_provider(**kwargs)
    raw = provider.complete(
        messages=[{"role": "user", "content": transcript}],
        system=system,
        max_tokens=max_tokens,
    )
    raw = _strip_code_fence(raw)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("proposer returned non-JSON; returning empty proposal")
        return {"nodes": [], "edges": []}

    existing_ids = {n.get("id", "") for n in backend.all_nodes()}
    proposal = _coerce_proposal(payload, existing_ids)
    if isinstance(payload.get("reasoning"), str):
        proposal["reasoning"] = payload["reasoning"]
    return proposal


def commit_proposal(
    proposal: dict,
    source: str,
    backend: GraphBackend,
) -> dict:
    """Write proposal nodes + edges to the backend, all flagged provisional
    under `source`. Returns counts."""
    nodes_added = 0
    edges_added = 0
    existing_ids = {n.get("id", "") for n in backend.all_nodes()}
    for node in proposal.get("nodes") or []:
        if node["id"] not in existing_ids:
            backend.add_node(node, provisional_source=source)
            nodes_added += 1
    for edge in proposal.get("edges") or []:
        backend.add_edge(edge, provisional_source=source)
        edges_added += 1
    return {"nodes_added": nodes_added, "edges_added": edges_added, "source": source}
