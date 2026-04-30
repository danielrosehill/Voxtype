# Evolution: From Voice Notepad V3 to Voxtype

## The Core Insight That Started It All

Traditional voice-to-text is a two-stage process: first an ASR (Automatic Speech Recognition) model transcribes audio to text, then optionally an LLM cleans it up. This approach has an inherent problem — the ASR model outputs literal speech, full of "ums", broken sentences, and no formatting, and the cleanup LLM is working with text that has already lost the audio context.

**Voice Notepad V3** (later renamed AI Transcription Notepad) was built around a different idea: send the raw audio directly to a multimodal AI model along with a text prompt describing how to clean it up. The model handles both transcription and cleanup in a single pass. This turned out to work remarkably well — particularly with Google's Gemini Flash models, which proved both fast and cheap for this use case (a few dollars for thousands of transcriptions via OpenRouter).

## What V3 Built

V3 evolved into a full-featured desktop application over months of daily use and iteration:

### The Good Stuff (proven and valuable)
- **Multimodal transcription pipeline**: Audio → Gemini via OpenRouter → clean text. Single pass. This is the core innovation and it works.
- **Audio processing pipeline**: Automatic Gain Control (AGC) normalizes quiet recordings. Voice Activity Detection (TEN VAD) strips silence before upload, reducing cost and latency. 16kHz mono compression matches Gemini's internal format.
- **Global hotkeys via evdev**: Works on Wayland (where most Linux hotkey solutions fail). Reads directly from input-remapper devices, making it compatible with USB macropads and programmable keyboards. F13-F24 keys avoid conflicts with standard shortcuts.
- **Multiple hotkey functions**: Toggle (one-button workflow), tap-toggle + transcribe + append (multi-segment workflow), pause, clear. These support different usage patterns from "quick note" to "long dictation session."
- **Text injection**: Types transcribed text directly at the cursor position via ydotool. Essential for using the app as an invisible typing assistant.
- **Audio feedback**: PTT walkie-talkie beeps for recording events (procedurally generated, no external dependencies). TTS voice announcements for accessibility. Three modes: beeps, voice, silent.
- **Cleanup prompt**: A carefully tuned prompt that handles filler words, repetitions, trailing sentences, meta-instructions ("scratch that"), spelling clarifications ("Z-O-D"), punctuation, paragraphing, and grammar — while preserving the speaker's voice and intent.
- **Second-pass review agent**: A cheap model does a second text-only pass to catch misheard words (acoustic similarity errors like "lava" → "law") and fix semantic coherence issues the first pass missed.
- **AI preamble stripping**: Defense-in-depth against Gemini prepending "Here is your transcription:" despite being told not to.

### Where V3 Got Complicated

The app grew a layered prompt system that tried to give users fine-grained control over the transcription output:

- **Format presets**: General, Email, Todo, Meeting Notes, Grocery List, Bullet Points, Technical Docs, AI Prompt, Dev Prompt, Cover Letter, Verbatim, and more
- **Formality levels**: Casual, Neutral, Professional
- **Verbosity reduction**: None, Minimum, Short, Medium, Maximum
- **Prompt checkboxes**: Follow verbal instructions, add subheadings, use markdown, remove unintentional dialogue, enhance AI prompts
- **Prompt stacks**: A system for layering multiple prompt modifiers
- **Prompt library**: A database-backed system for saving and loading custom prompts
- **Stack builder**: A visual UI for composing prompt elements
- **Writing samples**: One-shot style copying from user-provided text
- **Translation mode**: 30+ languages with auto-detect

The problem was reliability. The model either over-edited (reformatting things that didn't need reformatting) or under-edited (ignoring formatting instructions). Getting it to reliably apply "professional email format" without mangling a quick note was a constant battle. The more control surface area, the more edge cases.

### The UI Problem

The UI grew to match the feature set:
- **7 tabs**: Record, History, Cost, Analysis, Models, Prompt Stacks, About
- **Multiple settings panels**: Prompt, Personalization, Translation, Hotkeys, Mic, Behavior, Misc
- **Complex database**: Mongita (MongoDB-compatible) for transcription history with full metadata
- **Embedding system**: Gemini embeddings for semantic search across transcription history
- **Cost tracking**: Token-based estimates, OpenRouter balance polling, model-by-model breakdown
- **Analytics**: Performance metrics, daily activity charts, export to JSON

All useful individually, but collectively the app had become heavy and intimidating. For something whose core job is "press button, talk, get text," there was too much cognitive overhead.

## The V2 Reset

Voxtype keeps everything that was proven valuable and drops everything that added complexity without proportional benefit.

### What Changed

| Aspect | V3 | V2 |
|--------|----|----|
| **UI** | 7 tabs, multiple panels | Single window, no tabs |
| **Format control** | 12+ presets + formality + verbosity + checkboxes | Auto-detect default + simple dropdown override |
| **Prompt system** | 3-layer architecture + prompt stacks + prompt library | Single cleanup prompt + optional format/tone |
| **History** | Full database with metadata, semantic search, embeddings | In-memory session buffer (last 20, not persisted) |
| **Settings** | Multiple tabbed panels | One compact dialog |
| **Config file** | 60+ fields with migration logic for legacy fields | ~20 fields, no migration needed |
| **Cost tracking** | Token estimates, balance polling, model breakdown | None (check OpenRouter dashboard) |
| **TTS voices** | 6 character voice packs | Single voice (Ryan) |
| **Translation** | 30+ languages with auto-detect | Dropped |
| **Analytics** | Charts, model performance, export | Dropped |

### What Stayed

Everything in the "proven and valuable" list above. The audio pipeline, hotkeys, text injection, audio feedback, cleanup prompt, review agent, preamble stripping, output mode flexibility — all carried over from V3.

### The Key Design Decision

**Auto-detect is the default.** Instead of asking the user to pick a format before they speak, the model figures it out. If you dictate a shopping list, it comes back as bullet points. If you dictate an email, it gets a greeting and sign-off. The format dropdown is there as an override, not a requirement.

This is more aligned with how dictation actually works: you usually know what you want to say, but you don't always know what format it is until you've said it. Let the AI figure that part out.

### What Could Come Back

Some V3 features were useful but could be re-implemented more simply:
- **Translation**: Could be a single "translate to: [language]" dropdown rather than a full translation mode
- **Persistent history**: Could use a simple SQLite table rather than MongoDB + embeddings
- **Cost tracking**: A simple per-session counter rather than the full polling/analytics system
- **Writing samples**: Could be a "match this style" text field in settings rather than a separate UI panel

The goal is to add these only if needed, and to keep them frictionless when added.

## Technical Lineage

Both projects use the same core libraries:
- **PyQt6** for the desktop UI
- **PyAudio** for microphone recording
- **pydub** for audio format conversion
- **TEN VAD** for voice activity detection
- **OpenAI SDK** (pointed at OpenRouter's API) for multimodal transcription
- **pynput + evdev** for global hotkeys
- **ydotool** for text injection on Wayland

The Gemini models are accessed through OpenRouter rather than directly because OpenRouter consistently provides lower latency (tested over ~2000 transcriptions in V3 development).
