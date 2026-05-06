#!/bin/bash
# Download the default Whisper model used by Sponge.
# Idempotent — skips if the file already exists.
# Override target via WHISPER_MODEL env var; defaults to ./models/ggml-base.en.bin.
set -euo pipefail

DEFAULT_PATH="$(pwd)/models/ggml-base.en.bin"
TARGET="${WHISPER_MODEL:-$DEFAULT_PATH}"
URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"

if [[ -f "$TARGET" ]]; then
    SIZE=$(du -h "$TARGET" | cut -f1)
    echo "[install_whisper] Already present at $TARGET ($SIZE) — skipping."
    exit 0
fi

mkdir -p "$(dirname "$TARGET")"
echo "[install_whisper] Downloading $URL → $TARGET (~140 MB)"
if command -v curl >/dev/null 2>&1; then
    curl -L --fail --progress-bar -o "$TARGET" "$URL"
elif command -v wget >/dev/null 2>&1; then
    wget --progress=bar -O "$TARGET" "$URL"
else
    echo "[install_whisper] error: neither curl nor wget available." >&2
    exit 1
fi

SIZE=$(du -h "$TARGET" | cut -f1)
echo "[install_whisper] Done. Model at $TARGET ($SIZE)."
echo "[install_whisper] Sponge will pick this up automatically — no env var needed."
