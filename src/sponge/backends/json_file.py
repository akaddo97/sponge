"""JsonFileBackend — simplest working GraphBackend impl.

Stores everything in a single JSON file:
    {"directed": true, "nodes": [...], "edges": [...]}

Atomic write via temp-file rename. fcntl lock for cross-process safety
(single Flask process today, but the lock costs nothing and protects
against a future watcher running in parallel).

Use it for prototyping and personal-scale graphs (up to ~10k nodes).
For larger graphs, implement GraphBackend against a real database.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


class JsonFileBackend:
    """Reference GraphBackend backed by a single JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write({"directed": True, "nodes": [], "edges": []})

    # --- private helpers ---

    def _read(self) -> dict:
        with self.path.open("r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        # Some serialisers use "links" instead of "edges"; accept both.
        if "edges" not in data and "links" in data:
            data["edges"] = data.pop("links")
        data.setdefault("directed", True)
        data.setdefault("nodes", [])
        data.setdefault("edges", [])
        return data

    def _write(self, data: dict) -> None:
        # Atomic write: serialise to a temp file in the same dir, then rename.
        # rename is atomic on POSIX so readers never see a partial file.
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=self.path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _stamp_provisional(record: dict, source: str | None) -> dict:
        if source:
            record["verified"] = False
            record["provisional_source"] = source
            record["provisional_added_at"] = _now_iso()
            record["provisional_added_ts"] = _now_ts()
        else:
            record.setdefault("verified", True)
        return record

    # --- public node ops ---

    def add_node(self, node: dict, *, provisional_source: str | None = None) -> str:
        if "id" not in node:
            raise ValueError("node must include 'id'")
        data = self._read()
        existing = next((n for n in data["nodes"] if n["id"] == node["id"]), None)
        if existing:
            existing.update(node)
            self._stamp_provisional(existing, provisional_source)
        else:
            new_node = dict(node)
            self._stamp_provisional(new_node, provisional_source)
            data["nodes"].append(new_node)
        self._write(data)
        return node["id"]

    def get_node(self, node_id: str) -> dict | None:
        for n in self._read()["nodes"]:
            if n["id"] == node_id:
                return n
        return None

    def update_node(self, node_id: str, fields: dict) -> bool:
        data = self._read()
        for n in data["nodes"]:
            if n["id"] == node_id:
                n.update(fields)
                self._write(data)
                return True
        return False

    def all_nodes(self) -> Iterator[dict]:
        for n in self._read()["nodes"]:
            yield n

    # --- public edge ops ---

    def add_edge(self, edge: dict, *, provisional_source: str | None = None) -> None:
        if "source" not in edge or "target" not in edge:
            raise ValueError("edge must include 'source' and 'target'")
        data = self._read()
        triple = (edge["source"], edge["target"], edge.get("relation", ""))
        for existing in data["edges"]:
            if (
                existing.get("source"),
                existing.get("target"),
                existing.get("relation", ""),
            ) == triple:
                return
        new_edge = dict(edge)
        self._stamp_provisional(new_edge, provisional_source)
        data["edges"].append(new_edge)
        self._write(data)

    def all_edges(self) -> Iterator[dict]:
        for e in self._read()["edges"]:
            yield e

    # --- provisional lifecycle ---

    def find_provisional(
        self,
        source: str | None = None,
        since_ts: float | None = None,
    ) -> dict:
        data = self._read()

        def matches(record: dict) -> bool:
            if record.get("verified", True):
                return False
            if source is not None and record.get("provisional_source") != source:
                return False
            if since_ts is not None:
                if (record.get("provisional_added_ts") or 0) < since_ts:
                    return False
            return True

        return {
            "nodes": [n for n in data["nodes"] if matches(n)],
            "edges": [e for e in data["edges"] if matches(e)],
        }

    def commit_provisional(self, source: str) -> int:
        data = self._read()
        flipped = 0
        for record in data["nodes"] + data["edges"]:
            if (
                not record.get("verified", True)
                and record.get("provisional_source") == source
            ):
                record["verified"] = True
                record.pop("provisional_source", None)
                record.pop("provisional_added_at", None)
                record.pop("provisional_added_ts", None)
                flipped += 1
        if flipped:
            self._write(data)
        return flipped

    def reject_provisional(self, source: str) -> int:
        data = self._read()
        before_n = len(data["nodes"])
        before_e = len(data["edges"])
        data["nodes"] = [
            n for n in data["nodes"]
            if n.get("verified", True) or n.get("provisional_source") != source
        ]
        # Drop edges with this source AND any edges referencing rejected nodes.
        rejected_node_ids = {
            n["id"] for n in self._read()["nodes"]
            if n.get("provisional_source") == source and not n.get("verified", True)
        } - {n["id"] for n in data["nodes"]}
        data["edges"] = [
            e for e in data["edges"]
            if (e.get("verified", True) or e.get("provisional_source") != source)
            and e.get("source") not in rejected_node_ids
            and e.get("target") not in rejected_node_ids
        ]
        removed = (before_n - len(data["nodes"])) + (before_e - len(data["edges"]))
        if removed:
            self._write(data)
        return removed

    # --- search ---

    def find_nodes_by_label(self, query: str, limit: int = 10) -> list[dict]:
        q = query.lower().strip()
        if not q:
            return []
        out: list[dict] = []
        for n in self._read()["nodes"]:
            label = n.get("label", "")
            aliases = n.get("aliases") or []
            if q in label.lower() or any(q in a.lower() for a in aliases):
                out.append(n)
                if len(out) >= limit:
                    break
        return out

    def stats(self) -> dict:
        data = self._read()
        prov_n = sum(1 for n in data["nodes"] if not n.get("verified", True))
        prov_e = sum(1 for e in data["edges"] if not e.get("verified", True))
        return {
            "node_count": len(data["nodes"]),
            "edge_count": len(data["edges"]),
            "provisional_node_count": prov_n,
            "provisional_edge_count": prov_e,
        }
