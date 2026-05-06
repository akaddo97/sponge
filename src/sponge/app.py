"""Flask app — Sponge HTTP surface.

Routes:
    GET  /                       → home (Jazz UI)
    GET  /verify                 → verify pane (provisional review)
    POST /api/voice/upload       → multipart audio → pipeline → reply
    GET  /api/sponge/stats       → graph stats for the dashboard
    GET  /api/sponge/topics      → topic chips for the home grid (v0.1: empty)
    GET  /api/sponge/terminal/feed → SSE event stream (v0.1: heartbeat-only)
    POST /api/verify/apply       → flip a single event's provisional → verified
    POST /api/verify/reject      → drop a single event's provisional entries
    POST /api/verify/apply_batch → flip every provisional with this prefix
    POST /api/verify/reject_batch→ drop every provisional with this prefix
    POST /api/query/mobile       → SSE chat (v0.1: routes user text to the briefer)

The agentic tool-using `/query` loop in the parent project is NOT in v0.1.
Voice → graph → verify → briefer is the v0.1 happy path.

Configuration: pass a GraphBackend to `create_app(backend=...)`. Default uses
`JsonFileBackend(SPONGE_GRAPH_PATH or ./graph.json)`.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from flask import Flask, Response, jsonify, render_template, request

from sponge.backends.json_file import JsonFileBackend
from sponge.chat_briefer import brief
from sponge.graph_backend import GraphBackend
from sponge.transcription import get_transcriber, parse_voice_memo_markdown
from sponge.voice_cleaner import clean_transcript
from sponge.voice_pipeline import process_audio, process_markdown_sidecar

UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024  # 25 MB — well above a 5-min memo


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_added_at(ts: str | None) -> str:
    """Convert ISO timestamp to a relative 'today / 3d ago' label."""
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    delta = datetime.now(timezone.utc) - when
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    return "today" if days == 0 else f"{days}d ago"


def _group_provisional(backend: GraphBackend) -> tuple[list[dict], list[dict]]:
    """Group provisional nodes/edges by source.

    Returns (events, batches):
        events: per-source dicts for the verify pane (single voice memos)
        batches: aggregated by source-prefix (bulk imports — out of v0.1
                 scope but the template renders them when present)
    """
    by_source: dict[str, dict] = defaultdict(lambda: {
        "nodes": [],
        "edges": [],
        "added_at": "",
    })
    nodes_by_id = {n["id"]: n for n in backend.all_nodes()}

    for n in backend.all_nodes():
        if n.get("verified", True):
            continue
        src = n.get("provisional_source", "(unknown)")
        bucket = by_source[src]
        bucket["nodes"].append(n)
        if n.get("provisional_added_at") and not bucket["added_at"]:
            bucket["added_at"] = n["provisional_added_at"]

    for e in backend.all_edges():
        if e.get("verified", True):
            continue
        src = e.get("provisional_source", "(unknown)")
        bucket = by_source[src]
        # Pre-resolve source/target labels for the template.
        src_label = nodes_by_id.get(e["source"], {}).get("label", e["source"])
        tgt_label = nodes_by_id.get(e["target"], {}).get("label", e["target"])
        bucket["edges"].append({
            **e,
            "key": f'{e["source"]}->{e["target"]}:{e.get("relation", "")}',
            "source_label": src_label,
            "target_label": tgt_label,
        })
        if e.get("provisional_added_at") and not bucket["added_at"]:
            bucket["added_at"] = e["provisional_added_at"]

    events = []
    for src, bucket in sorted(by_source.items(), key=lambda kv: kv[1]["added_at"], reverse=True):
        events.append({
            "provisional_source": src,
            "nodes": bucket["nodes"],
            "edges": bucket["edges"],
            "node_count": len(bucket["nodes"]),
            "edge_count": len(bucket["edges"]),
            "added_at_display": _format_added_at(bucket["added_at"]),
        })
    return events, []


def _topics_from_backend(backend: GraphBackend, limit: int = 6) -> list[dict]:
    """Best-effort topic chips for the home dashboard.

    v0.1: empty (the source repo's topic extractor is out of scope).
    The home grid renders gracefully when this returns [].
    """
    return []


def _ssesend(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# --- factory ---


def create_app(backend: GraphBackend | None = None) -> Flask:
    """Build the Flask app. Call once per process."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent.parent.parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent.parent.parent / "static"),
    )
    app.config["MAX_CONTENT_LENGTH"] = UPLOAD_LIMIT_BYTES
    app.config["SPONGE_BACKEND"] = backend or JsonFileBackend(
        os.environ.get("SPONGE_GRAPH_PATH", str(Path.cwd() / "graph.json"))
    )
    app.config["SPONGE_DATA_DIR"] = Path(
        os.environ.get("SPONGE_DATA_DIR", str(Path.cwd() / "data"))
    )

    # ── pages ────────────────────────────────────────────────────────────────

    @app.route("/")
    def home():
        return render_template("index.html")

    @app.route("/verify")
    def verify():
        backend = app.config["SPONGE_BACKEND"]
        events, batches = _group_provisional(backend)
        return render_template("verify.html", events=events, batches=batches)

    # ── voice intake ─────────────────────────────────────────────────────────

    @app.route("/api/inbox/audio_upload", methods=["POST"])
    def audio_upload():
        """Multipart audio → transcribe → clean → propose → commit → brief.

        v0.1 is sync (no async polling). Long memos block the request thread;
        keep memos under ~60s for the in-browser flow.
        """
        # Field name matches the JS FormData contract (`file`) used by the
        # mobile UI. The frontend appends as form.append('file', blob, ...).
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "audio file required (form field 'file')"}), 400
        upload = request.files["file"]
        if not upload.filename:
            return jsonify({"ok": False, "error": "filename required"}), 400

        data_dir = Path(app.config["SPONGE_DATA_DIR"])
        inbox_dir = data_dir / "voice_inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_stem = "".join(c for c in Path(upload.filename).stem if c.isalnum() or c in "_-")
        stem = f"{ts}_{safe_stem or 'memo'}"
        audio_path = inbox_dir / f"{stem}.m4a"
        upload.save(audio_path)

        try:
            result = process_audio(
                audio_path,
                app.config["SPONGE_BACKEND"],
                memo_dir=data_dir / "voice_memos" / stem,
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # Enrich proposal edges with source/target labels so the inline
        # verify-card in the chat can render readable text without a
        # second round-trip. New nodes carry their own label in the
        # proposal; existing nodes are looked up via the backend.
        proposal = dict(result.proposal or {})
        node_labels = {n.get("id"): n.get("label", n.get("id", ""))
                       for n in proposal.get("nodes", []) if n.get("id")}
        for n in app.config["SPONGE_BACKEND"].all_nodes():
            nid = n.get("id")
            if nid and nid not in node_labels:
                node_labels[nid] = n.get("label", nid)
        proposal["edges"] = [
            {**e,
             "source_label": node_labels.get(e.get("source", ""), e.get("source", "")),
             "target_label": node_labels.get(e.get("target", ""), e.get("target", ""))}
            for e in proposal.get("edges", [])
        ]
        return jsonify({
            "ok": True,
            "memo_id": result.memo_id,
            "user_transcript": result.cleaned.text,
            "briefer_text": result.briefer_reply,
            "commit_summary": result.commit_summary,
            "proposal": proposal,
        })

    @app.route("/api/voice/job/<job_id>")
    def voice_job(job_id):
        # v0.1 is sync; the async polling endpoint is reserved for v0.2.
        return jsonify({"ok": False, "error": "async voice jobs are not in v0.1"}), 501

    # ── dashboard / topics / terminal ────────────────────────────────────────

    @app.route("/api/sponge/stats")
    @app.route("/api/sponge/dashboard")
    def sponge_stats():
        stats = app.config["SPONGE_BACKEND"].stats()
        return jsonify({
            "ok": True,
            "nodes": stats["node_count"],
            "edges": stats["edge_count"],
            "provisional_nodes": stats["provisional_node_count"],
            "provisional_edges": stats["provisional_edge_count"],
            "captured_at": _now_iso(),
        })

    @app.route("/api/sponge/topics")
    def sponge_topics():
        return jsonify({"ok": True, "topics": _topics_from_backend(app.config["SPONGE_BACKEND"])})

    @app.route("/api/sponge/terminal/feed")
    def sponge_terminal_feed():
        """SSE stream — heartbeat only in v0.1.

        The home page subscribes to keep the stats footer alive. Real-time
        ingest events are emitted by `process_audio` callers in v0.2.
        """
        def gen() -> Iterable[str]:
            yield _ssesend({"type": "hello", "ts": _now_iso()})
            while True:
                time.sleep(15)
                yield _ssesend({"type": "heartbeat", "ts": _now_iso()})
        return Response(gen(), mimetype="text/event-stream")

    @app.route("/api/voice/pending")
    def voice_pending_stream():
        # v0.1 doesn't ship the chat-prefixed memo branch yet.
        def gen() -> Iterable[str]:
            yield _ssesend({"type": "hello", "ts": _now_iso()})
            while True:
                time.sleep(30)
                yield _ssesend({"type": "heartbeat", "ts": _now_iso()})
        return Response(gen(), mimetype="text/event-stream")

    @app.route("/api/voice/pending/consume", methods=["POST"])
    def voice_pending_consume():
        return jsonify({"ok": True, "consumed": 0})

    # ── verify ───────────────────────────────────────────────────────────────

    @app.route("/api/verify/apply", methods=["POST"])
    def verify_apply():
        body = request.get_json(silent=True) or {}
        source = body.get("provisional_source")
        if not source:
            return jsonify({"ok": False, "error": "provisional_source required"}), 400
        flipped = app.config["SPONGE_BACKEND"].commit_provisional(source)
        return jsonify({"ok": True, "flipped": flipped})

    @app.route("/api/verify/reject", methods=["POST"])
    def verify_reject():
        body = request.get_json(silent=True) or {}
        source = body.get("provisional_source")
        if not source:
            return jsonify({"ok": False, "error": "provisional_source required"}), 400
        removed = app.config["SPONGE_BACKEND"].reject_provisional(source)
        return jsonify({"ok": True, "removed": removed})

    @app.route("/api/verify/apply_batch", methods=["POST"])
    def verify_apply_batch():
        body = request.get_json(silent=True) or {}
        prefix = body.get("prefix") or ""
        if not prefix:
            return jsonify({"ok": False, "error": "prefix required"}), 400
        flipped = 0
        sources_seen: set[str] = set()
        for n in app.config["SPONGE_BACKEND"].all_nodes():
            src = n.get("provisional_source", "")
            if src and src.startswith(prefix) and src not in sources_seen:
                sources_seen.add(src)
        for src in sources_seen:
            flipped += app.config["SPONGE_BACKEND"].commit_provisional(src)
        return jsonify({"ok": True, "flipped": flipped, "sources": list(sources_seen)})

    @app.route("/api/verify/reject_batch", methods=["POST"])
    def verify_reject_batch():
        body = request.get_json(silent=True) or {}
        prefix = body.get("prefix") or ""
        if not prefix:
            return jsonify({"ok": False, "error": "prefix required"}), 400
        removed = 0
        sources_seen: set[str] = set()
        for n in app.config["SPONGE_BACKEND"].all_nodes():
            src = n.get("provisional_source", "")
            if src and src.startswith(prefix) and src not in sources_seen:
                sources_seen.add(src)
        for src in sources_seen:
            removed += app.config["SPONGE_BACKEND"].reject_provisional(src)
        return jsonify({"ok": True, "removed": removed, "sources": list(sources_seen)})

    # ── chat (text input from the home search bar) ───────────────────────────

    @app.route("/api/query/mobile", methods=["POST"])
    def chat_query():
        """SSE chat — v0.1 routes typed input to the briefer.

        The agentic tool-using loop from the parent project is reserved for
        v0.2. v0.1 just acknowledges typed input; voice memos are the canonical
        graph-mutation surface.
        """
        # Accept both `message` (current JS contract — see static/js/sponge.js)
        # and `query` (legacy/parent-project name) so callers don't have to know
        # which one this version expects.
        body = request.get_json(silent=True) or {}
        text = (body.get("message") or body.get("query") or "").strip()
        if not text:
            return Response(_ssesend({"type": "error", "error": "empty query"}), mimetype="text/event-stream")

        def gen() -> Iterable[str]:
            yield _ssesend({"type": "hello"})
            try:
                reply = brief(text, {"nodes": [], "edges": []})
            except Exception as exc:
                yield _ssesend({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                return
            yield _ssesend({"type": "text", "text": reply})
            yield _ssesend({"type": "stop", "stop_reason": "end_turn"})
        return Response(gen(), mimetype="text/event-stream")

    # ── health ───────────────────────────────────────────────────────────────

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "ts": _now_iso()})

    return app


def main() -> int:  # pragma: no cover
    app = create_app()
    host = os.environ.get("SPONGE_HOST", "127.0.0.1")
    port = int(os.environ.get("SPONGE_PORT", "5050"))
    debug = bool(os.environ.get("SPONGE_DEBUG"))
    app.run(host=host, port=port, debug=debug, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
