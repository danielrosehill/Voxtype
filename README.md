# Voxtype

Voice dictation with multimodal AI cleanup. Speak naturally, get polished text.

## What It Does

Records your voice, sends the audio to a multimodal AI model (Gemini via OpenRouter), and gets back clean, well-formatted text in a single pass. No separate speech-to-text step — the AI handles both transcription and cleanup simultaneously.

The model automatically detects what you're dictating (email, shopping list, meeting notes, etc.) and formats it appropriately. You can also force a specific format if you want.

## Key Features

- **Single-pass multimodal transcription** — audio goes directly to Gemini, which transcribes AND cleans up in one API call
- **Smart format detection** — the model figures out if you're dictating an email, a list, notes, etc.
- **Voice Activity Detection (VAD)** — strips silence before sending to the API (saves cost and time)
- **Automatic Gain Control (AGC)** — normalizes audio levels for consistent results
- **Second-pass review** — optional coherence check catches misheard words
- **Custom dictionary** — post-processing substitutions for words the model consistently mishears (names, jargon, acronyms). Import/export as CSV for portability with other dictation tools. See [docs/dictionary-format.md](docs/dictionary-format.md).
- **Global hotkeys** — works system-wide, even when the app is minimized (F13-F24 keys)
- **Append mode** — record multiple segments, then transcribe them together
- **Output flexibility** — show in window, copy to clipboard, or type at cursor (three independent toggles; each bindable to a hotkey)
- **Streaming transcription** — live partial text while the model generates, lowering perceived latency
- **Type-at-cursor via Ctrl+Shift+V** — works in terminals (Konsole, Claude Code CLI, VS Code) as well as GUI apps. See [docs/keyboard-emulation.md](docs/keyboard-emulation.md) for details.

## Quick Start

```bash
# Clone and run
git clone https://github.com/danielrosehill/AI-Typer-V2.git
cd AI-Typer-V2
chmod +x run.sh
./run.sh
```

On first run, you'll be prompted to enter your [OpenRouter API key](https://openrouter.ai).

### System Dependencies (Ubuntu/Debian)

```bash
sudo apt install python3 python3-venv ffmpeg portaudio19-dev
# For VAD:
sudo apt install libc++1
# For clipboard:
sudo apt install wl-clipboard   # Wayland
# For text injection:
sudo apt install ydotool
```

## Usage

### Simple Workflow
1. Press **Record** (or your hotkey, default F13)
2. Speak naturally
3. Press **Stop** — transcription streams in as the model generates it

### Append Workflow
1. Press **F16** to start recording
2. Press **F16** again to stop and cache the audio
3. Press **F19** to record another segment
4. Press **F17** to transcribe all segments together
5. Press **F18** to clear the cache

### Output modes

Three independent toggles, visible in the output bar and bindable to hotkeys:

- **Show in window** — live-updates the text box as the model streams
- **Clipboard** — auto-copies the finished transcription
- **Type at cursor** — pastes at the cursor via clipboard + Ctrl+Shift+V (see [docs/keyboard-emulation.md](docs/keyboard-emulation.md))

Each can be flipped on/off without opening settings once a hotkey is assigned.

### Format & Tone
- **Format dropdown**: Auto-detect (default), General, Email, To-Do, Meeting Notes, Bullets, Technical, and more
- **Tone dropdown**: Casual, Neutral, Professional, Formal, Terse, and more
- These are applied at transcription time — you can change them between recording and transcribing

## Configuration

Settings are stored in `~/.config/ai-typer-v2/config.json`.

Access via **File → Settings** or **Ctrl+,**.

### Hotkeys

| Function | Default | Description |
|----------|---------|-------------|
| Toggle | F13 | Start recording, or stop and transcribe |
| Tap Toggle | F16 | Start recording, or stop and cache |
| Transcribe | F17 | Transcribe cached audio |
| Clear | F18 | Clear recording and cache |
| Append | F19 | Start a new recording segment |
| Pause | F20 | Pause/resume recording |
| Retake | F21 | Discard current recording and restart |
| Toggle window output | (unset) | Flip "Show in window" on/off |
| Toggle clipboard | (unset) | Flip "Clipboard" on/off |
| Toggle type-at-cursor | (unset) | Flip "Type at cursor" on/off |

Hotkeys work globally on Wayland via evdev (reads from input-remapper devices). Falls back to pynput/X11 on other systems.

## Compatible Models

### Benchmark results (in-house eval, 17/04/2026)

Full sweep across 4 dictation samples × 5 MP3 bitrates × 12 models (240 API calls). Full data: [`evals/results/full-sweep-1704-150152/summary.md`](evals/results/full-sweep-1704-150152/summary.md). Key findings that drive the app's defaults:

| Model | Best WER | Latency at 32 kbps | Notes |
|---|---:|---:|---|
| `mistralai/voxtral-small-24b-2507` | 0.017 | **1.25s** | **Recommended default** — 2-8× faster than Gemini, near-best accuracy |
| `google/gemini-3-flash-preview` | **0.007** | 2.20s | Accuracy-optimal alternative |
| `google/gemini-2.5-pro` | 0.014 | 6.64s | Strictly dominated — no reason to use for dictation |
| `openai/gpt-audio*` family | 0.014–0.541 | ~1.7s | Unstable — 25-40% conversationalization failure rate |

**Development direction**: Voxtral's latency lead is large enough that direct Mistral API support is now a first-class path (set `MISTRAL_API_KEY` or the Mistral key field in Settings to route Voxtral traffic direct, bypassing OpenRouter). OpenRouter remains the fallback and the route for all non-Mistral models.

### Model catalog

Voxtype works with any OpenRouter model that accepts audio input and produces text output. Models exposed in the settings UI are curated from OpenRouter's audio-input catalog — see [docs/openrouter-audio-models.md](docs/openrouter-audio-models.md) for the full snapshot and selection rationale. Current picks:

**Budget tier**
- `google/gemini-2.0-flash-lite-001` — cheapest ($0.075/M in)
- `google/gemini-2.0-flash-001`
- `google/gemini-2.5-flash-lite`
- `google/gemini-3.1-flash-lite-preview` *(default)*
- `mistralai/voxtral-small-24b-2507`

**Standard tier**
- `google/gemini-2.5-flash`
- `google/gemini-3-flash-preview`
- `xiaomi/mimo-v2-omni`
- `openai/gpt-audio-mini`
- `google/gemini-2.5-pro`
- `openai/gpt-audio`
- `openai/gpt-4o-audio-preview`

Browse the full OpenRouter catalog at [openrouter.ai/models](https://openrouter.ai/models?input_modalities=audio&output_modalities=text).

## Architecture

```
app/src/
├── main.py              # PyQt6 UI (single window, no tabs)
├── config.py            # Configuration and prompt building
├── audio_recorder.py    # PyAudio recording
├── audio_processor.py   # AGC + VAD + compression pipeline
├── vad_processor.py     # TEN VAD silence removal
├── transcription.py     # OpenRouter API client
├── hotkeys.py           # Global hotkeys (evdev + pynput)
└── clipboard.py         # Clipboard operations
```

## How It Works

1. **Record** audio from microphone (PyAudio)
2. **VAD** strips silence segments (TEN VAD)
3. **AGC** normalizes volume levels
4. **Compress** to 16kHz mono MP3 @ 64 kbps
5. **Send** audio + cleanup prompt to the selected multimodal model via OpenRouter (streaming SSE by default)
6. **Review** (optional) — second pass catches misheard words
7. **Output** — show in window (live-updates while streaming), copy to clipboard, and/or type at cursor

## License

MIT
