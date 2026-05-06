"""Polling watcher for the voice-memo inbox directory.

Watches a folder (typically `~/Library/Mobile Documents/<your-icloud-id>/Documents/voice_inbox`
when used with the iCloud iOS Shortcut). Every 3 seconds it scans for `.md`
files paired with `.m4a` audio. A file is processed when its size has been
stable for two consecutive ticks (the debounce defends against partial
iCloud syncs).

On success, files move to `archive/voice_memos/YYYY-MM/`. On failure, files
move to `inputs/voice_memos/failed/` so the watcher doesn't loop on them.

Run as a long-lived process (foreground for development, launchd agent
for production). See `launchd/com.sponge.voice-watcher.plist` for the
macOS user-agent setup.

Configure via env vars or constructor:
    SPONGE_INBOX        — directory to watch (required)
    SPONGE_GRAPH_PATH   — JSON file backing JsonFileBackend
    SPONGE_DATA_DIR     — where per-memo dirs land
    SPONGE_TICK_SECONDS — defaults to 3
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sponge.backends.json_file import JsonFileBackend
from sponge.voice_pipeline import process_markdown_sidecar

log = logging.getLogger("sponge.voice_watcher")


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


class VoiceWatcher:
    def __init__(
        self,
        inbox: Path,
        backend: JsonFileBackend,
        *,
        archive_dir: Path | None = None,
        failed_dir: Path | None = None,
        tick_seconds: float = 3.0,
        debounce_ticks: int = 2,
    ) -> None:
        self.inbox = Path(inbox)
        self.backend = backend
        self.archive_dir = Path(archive_dir) if archive_dir else Path.cwd() / "archive" / "voice_memos"
        self.failed_dir = Path(failed_dir) if failed_dir else Path.cwd() / "inputs" / "voice_memos" / "failed"
        self.tick_seconds = tick_seconds
        self.debounce_ticks = debounce_ticks
        self._stable: dict[Path, tuple[int, int]] = {}  # path → (last_size, stable_count)
        self._stop = False

    def stop(self, *_args) -> None:
        self._stop = True

    def _is_stable(self, path: Path) -> bool:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self._stable.pop(path, None)
            return False
        last, count = self._stable.get(path, (-1, 0))
        if size == last:
            count += 1
        else:
            count = 0
        self._stable[path] = (size, count)
        return count >= self.debounce_ticks

    def _archive(self, md_path: Path) -> None:
        yyyy_mm = datetime.now(timezone.utc).strftime("%Y-%m")
        target = self.archive_dir / yyyy_mm
        target.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(md_path), str(target / md_path.name))
        except OSError as exc:
            log.warning("archive failed for %s: %s", md_path, exc)
        audio = md_path.with_suffix(".m4a")
        if audio.exists():
            try:
                shutil.move(str(audio), str(target / audio.name))
            except OSError as exc:
                log.warning("archive failed for %s: %s", audio, exc)

    def _quarantine(self, md_path: Path) -> None:
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(md_path), str(self.failed_dir / md_path.name))
        except OSError:
            pass
        audio = md_path.with_suffix(".m4a")
        if audio.exists():
            try:
                shutil.move(str(audio), str(self.failed_dir / audio.name))
            except OSError:
                pass

    def _process_one(self, md_path: Path) -> None:
        log.info("processing %s", md_path.name)
        try:
            result = process_markdown_sidecar(md_path, self.backend)
        except Exception as exc:
            log.exception("pipeline failed for %s — quarantining: %s", md_path.name, exc)
            self._quarantine(md_path)
            return
        log.info(
            "memo %s: +%d nodes, +%d edges (provisional)",
            result.memo_id,
            result.commit_summary.get("nodes_added", 0),
            result.commit_summary.get("edges_added", 0),
        )
        self._archive(md_path)
        self._stable.pop(md_path, None)

    def tick(self) -> int:
        """Scan the inbox once. Returns the number of files processed."""
        if not self.inbox.exists():
            return 0
        processed = 0
        for md_path in sorted(self.inbox.glob("*.md")):
            if self._is_stable(md_path):
                self._process_one(md_path)
                processed += 1
        return processed

    def run(self) -> None:
        log.info("watcher running on %s (tick=%.1fs)", self.inbox, self.tick_seconds)
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        while not self._stop:
            self.tick()
            time.sleep(self.tick_seconds)
        log.info("watcher stopped")


def main() -> int:
    inbox = os.environ.get("SPONGE_INBOX")
    if not inbox:
        print("SPONGE_INBOX is required (path to the voice-memo inbox dir)", file=sys.stderr)
        return 2
    graph_path = os.environ.get("SPONGE_GRAPH_PATH", str(Path.cwd() / "graph.json"))
    backend = JsonFileBackend(graph_path)

    tick = float(os.environ.get("SPONGE_TICK_SECONDS", "3"))
    _setup_logging(verbose=bool(os.environ.get("SPONGE_VERBOSE")))

    watcher = VoiceWatcher(Path(inbox), backend, tick_seconds=tick)
    watcher.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
