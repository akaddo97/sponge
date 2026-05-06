# Install on macOS

End-to-end setup: voice memo on iPhone → graph mutation proposed on your Mac.
The whole pipeline is local except for the LLM proposal call (one round-trip
to Claude / Gemini / OpenAI of your choice).

## Prerequisites

- macOS 13+
- Python 3.11+
- An Apple ID with iCloud Drive enabled on Mac and iPhone
- An API key from at least one of: Anthropic, Google AI Studio, OpenAI

## 1. Clone + install

```bash
git clone https://github.com/akaddo97/sponge ~/Projects/sponge
cd ~/Projects/sponge
uv venv
uv pip install -e ".[whisper,dev]"
```

## 2. Whisper model

Sponge uses Whisper.cpp via `pywhispercpp` for local transcription. Download
a model into `./models/`:

```bash
mkdir -p models
curl -L -o models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

Other sizes available on the [whisper.cpp model page](https://huggingface.co/ggerganov/whisper.cpp).
`base.en` is ~150 MB and good enough for crisp dictation; `small.en` is
~470 MB and noticeably better on background noise.

To skip Whisper entirely (rely on the iOS Shortcut's on-device speech-to-text
written to a markdown sidecar), set `TRANSCRIBER=markdown_sidecar`.

## 3. LLM provider

Pick one and export the matching env var:

```bash
# Claude (default)
export ANTHROPIC_API_KEY=sk-ant-...
export LLM_PROVIDER=claude

# Gemini
export GEMINI_API_KEY=...
export LLM_PROVIDER=gemini

# OpenAI
export OPENAI_API_KEY=sk-...
export LLM_PROVIDER=openai
```

## 4. Boot the demo

```bash
cp examples/demo_graph/graph.json graph.json
sponge
```

Open `http://127.0.0.1:5050` in Safari. You should see the Sponge home page
with the dashboard tiles populated from the demo graph (12 people, 8 projects,
some teams).

## 5. iOS Shortcut

The iCloud-based pipeline relies on a Shortcut you build once on iPhone.

1. Open the **Shortcuts** app on iPhone → tap **+** to create a new Shortcut.
2. Add the following actions in order:
   - **Record Audio** (set length: *Ask Each Time*)
   - **Transcribe Audio** (uses Apple's on-device speech-to-text)
   - **Get File** → save the audio output as a temporary file
   - **Save File** → save **into iCloud Drive**, in a folder called
     `voice_inbox/` (Sponge will watch this folder)
   - Optionally also: **Text** action with frontmatter like:
     ```
     ---
     captured_at: <Current Date as ISO 8601>
     transcript_engine: apple_on_device
     ---

     <Transcribed Text>
     ```
   - **Save File** of that text as `<audio-filename>.md` in the same folder
3. Add the Shortcut to your Home Screen (or pin it to the Lock Screen).

Each invocation drops a paired `.m4a` + `.md` into `voice_inbox/`. iCloud
syncs them to your Mac within 5–30 seconds.

## 6. Mac watcher (launchd)

The watcher polls the iCloud `voice_inbox/` and runs the Sponge pipeline on
each new memo.

```bash
# Edit the plist to your absolute paths first.
$EDITOR launchd/com.sponge.voice-watcher.plist
# Replace REPLACE_ME with /absolute/path/to/sponge and REPLACE_ME_INBOX
# with the iCloud Drive path (see below).

ln -s "$(pwd)/launchd/com.sponge.voice-watcher.plist" \
      ~/Library/LaunchAgents/com.sponge.voice-watcher.plist
launchctl load ~/Library/LaunchAgents/com.sponge.voice-watcher.plist
```

Your iCloud Drive path is typically:

```
/Users/<you>/Library/Mobile Documents/iCloud~com~apple~Shortcuts/Documents/voice_inbox
```

(The exact `iCloud~com~apple~Shortcuts` slug varies — open Finder, navigate
to iCloud Drive, find the Shortcut output folder, and copy the absolute path.)

Verify the watcher is running:

```bash
launchctl print gui/$(id -u)/com.sponge.voice-watcher | head -20
tail -f logs/voice_watcher.log
```

## 7. Phone access via Tailscale (optional but recommended)

To open `/verify` on your phone while away from your home Wi-Fi, install
[Tailscale](https://tailscale.com/) on Mac + phone, then visit
`http://<your-mac-name>.<tailnet>:5050` in Safari.

## Demo recording

To record the showcase gif:

1. Open `sponge.local:5050` (or your Tailscale URL) in iPhone Safari.
2. Open Mac → System Settings → Privacy & Security → enable Screen Recording
   for QuickTime if needed.
3. Open QuickTime → File → New Movie Recording → click the dropdown next to
   the record button → select your iPhone as the camera + microphone.
4. Hit record. On the iPhone:
   - Tap the mic on the home page.
   - Speak: "I just met Alice — she's the founder of Lantern Bough Games,
     working on a tile-based platformer called Pebble Garden."
   - Watch the chat stream show transcription + briefer reply.
   - Tap *verify →* in the top bar.
   - Tap *Verify all* on the new memo's batch.
   - Switch back to the home page; see node count tick up.
5. Stop recording. Trim to ~10 s in QuickTime. Convert to gif:

```bash
ffmpeg -i recording.mov \
  -vf "fps=15,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  docs/demo.gif
```

## Troubleshooting

**"Whisper model not found at …"** — download the model (step 2) or set
`WHISPER_MODEL=/absolute/path/to/ggml-base.en.bin`.

**Watcher logs empty / no files processed** — check the inbox path is right
(Finder → iCloud Drive → Shortcuts → voice_inbox). Verify the plist with
`launchctl print gui/$(id -u)/com.sponge.voice-watcher`.

**Provisional entries don't appear on `/verify`** — confirm `graph.json`
has provisional rows: `jq '.nodes | map(select(.verified == false)) | length' graph.json`.

**LLM proposal returns empty** — try a different provider via `LLM_PROVIDER`,
or check the cleaned transcript: `data/voice_memos/<memo_id>/cleaned.txt`.
The proposer is conservative on purpose; if the memo was a fleeting thought
it returns empty arrays.
