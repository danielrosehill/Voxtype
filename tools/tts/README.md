# tools/tts

Generator for Voxtype's TTS announcement prompts. Three voices ship as
selectable voice packs:

| voice    | backend           | runs on      | cost                    |
|----------|-------------------|--------------|-------------------------|
| `ryan`   | Edge TTS (en-GB)  | local CPU    | free                    |
| `corn`   | Chatterbox        | Modal (GPU)  | ~T4 seconds per request |
| `herman` | Chatterbox        | Modal (GPU)  | ~T4 seconds per request |

`ryan` is the bundled default. `corn` and `herman` reuse the public voice
conditionals from My-Weird-Prompts
(`https://ai-files.myweirdprompts.com/voices/{voice}/{voice}_conds.pt`) so no
`.pt` files need to ship with this repo.

The runtime picks a pack from `Config.tts_voice` (`"ryan"` | `"corn"` |
`"herman"`). Files are looked up in this order:

1. `app/assets/tts/voices/<voice>/<key>.wav`  (voice-pack override)
2. `app/assets/tts/<key>.wav`                 (default Ryan / fallback)

So a partial voice pack (just `audio_sent.wav` and `audio_sent_waiting.wav`,
say) is fine — anything missing falls back to Ryan.

## One-time setup

```bash
# only required to (re)generate the corn / herman packs
modal deploy tools/tts/modal_tts.py
```

## Regenerating prompts

```bash
# all 3 voices, both prompts → app/assets/tts/(voices/<v>/)*.wav
python -m tools.tts.generate_prompts

# just one voice
python -m tools.tts.generate_prompts --voice ryan
```

## Ad-hoc synthesis

```bash
python -m tools.tts.synthesize --list
python -m tools.tts.synthesize --text "Hello." --voice ryan --out hello.mp3
```

The CLI's standalone default-voice config lives at
`~/.config/noisy-voice-notes/tts.json` (legacy path; only affects the CLI's
own default for ad-hoc runs, not the Voxtype app's runtime voice pack).
