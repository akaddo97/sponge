# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Validation gate: pluggable `Validator` Protocol + shipped `DefaultValidator` (generic structural rules), wired into the commit boundary with snapshot-and-rollback. A commit that would corrupt the graph is rejected with HTTP 422 and the store is restored byte-identical. New `snapshot()`/`restore()`/`load_graph()` on the `GraphBackend` Protocol; `create_app(validator=...)` injection; `commit_proposal(..., validator=...)` guards the voice pipeline's provisional write. Exported `Validator`, `DefaultValidator`, `ValidationError`.
- README: status badges (CI, Python, License) under the title.
- `CHANGELOG.md` — this file.
- `.github/ISSUE_TEMPLATE/bug_report.md` and `.github/pull_request_template.md` — standard contribution scaffolding.
- CI matrix gains `macos-latest` (was Linux-only); see sub 3's `chore` commits on the refresh branch.
- `dependabot.yml` for weekly dep refresh.
- `.gitignore` hardening to keep large local data + model files out of the repo.

### Changed
- `JsonFileBackend.reject_provisional` is now a single-read operation — see refactor on the refresh branch.
- `_post_transcript` helper now shared by the audio-upload and markdown-paste paths.

### Fixed
- `JsonFileBackend.add_edge` now de-duplicates by `(source, target, relation)` so re-running a voice-memo against the same subject doesn't keep stacking identical edges.
- `/verify` batch routes now iterate edges as well as nodes (previously edges were silently skipped from the batch view).
- Hardened exception handlers around `audio_upload` and `chat_query` so a single bad request can't surface internal traces to the client.

### Security
- Author email in `pyproject.toml` switched to GitHub noreply — keeps the personal address out of the published wheel metadata.

## [0.1.0] — 2026-05-06

### Added
- Initial extract from internal tooling.
- Flask app exposing `/`, `/verify`, `/chat`, `/audio_upload`, `/proposal/<id>` over a personal knowledge graph.
- Voice-memo pipeline: iOS Shortcut → iCloud `voice_inbox/` → Mac watcher → on-device Whisper.cpp transcription → LLM-proposed graph mutations → provisional commit → inline verify card.
- `GraphBackend` Protocol with a reference `JsonFileBackend` — single-file, fcntl-locked, two-pass writes. BYOG framing throughout (see README "Bring your own graph").
- Provider abstraction for LLM calls — no hard dependency on a single vendor.
- Inline verify UI: ✓ Verify / ✗ Discard buttons directly under the model's chat reply; full `/verify` batch view available for catching up.
- pytest suite, CI on Ubuntu × Python 3.11 / 3.12 / 3.13.
- MIT license.

[Unreleased]: https://github.com/akaddo97/sponge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/akaddo97/sponge/releases/tag/v0.1.0
