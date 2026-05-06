#!/usr/bin/env python3
"""
Unified TTS CLI for the Noisy-Voice-Notes project.

Three voices, user-selectable, with a persistent default:
  - corn      Chatterbox via Modal (GPU)
  - herman    Chatterbox via Modal (GPU)
  - ryan      Microsoft Edge `en-US-RyanNeural` (local CPU, free)

Usage:
  python -m tts.synthesize --text "Hello there" --voice corn --out out.mp3
  python -m tts.synthesize --text-file note.md                       # uses default voice
  python -m tts.synthesize --list
  python -m tts.synthesize --set-default herman
  python -m tts.synthesize --get-default

Modal voices require `modal deploy tts/modal_tts.py` once. Ryan needs only
`pip install edge-tts` (already in the venv).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .voices import VOICES, DEFAULT_VOICE

CONFIG_PATH = Path.home() / ".config" / "noisy-voice-notes" / "tts.json"


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def get_default_voice() -> str:
    return _load_config().get("default_voice", DEFAULT_VOICE)


def set_default_voice(voice: str) -> None:
    if voice not in VOICES:
        raise SystemExit(f"unknown voice {voice!r}; choices: {list(VOICES)}")
    cfg = _load_config()
    cfg["default_voice"] = voice
    _save_config(cfg)
    print(f"default voice -> {voice}  ({CONFIG_PATH})")


# ---------------- backends ----------------

def _synth_chatterbox(text: str, voice: str, out: Path) -> None:
    """Call the deployed Modal app's TTS class remotely."""
    import modal
    print(f"[modal] dispatching to Chatterbox ({voice}) via deployed app…")
    TTS = modal.Cls.from_name("noisy-voice-tts", "TTS")
    data = TTS().synthesize.remote(text, voice)
    out.write_bytes(data)


def _synth_edge(text: str, voice: str, out: Path) -> None:
    import edge_tts
    voice_id = VOICES[voice]["edge_voice_id"]
    print(f"[edge-tts] {voice_id}  ({len(text)} chars)…")

    async def _run():
        comm = edge_tts.Communicate(text, voice_id)
        await comm.save(str(out))

    asyncio.run(_run())


BACKENDS = {
    "chatterbox": _synth_chatterbox,
    "edge": _synth_edge,
}


def synthesize(text: str, voice: str, out: Path) -> Path:
    if voice not in VOICES:
        raise SystemExit(f"unknown voice {voice!r}; choices: {list(VOICES)}")
    backend = VOICES[voice]["backend"]
    out.parent.mkdir(parents=True, exist_ok=True)
    BACKENDS[backend](text, voice, out)
    print(f"wrote {out}  ({out.stat().st_size:,} bytes)")
    return out


# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--text", type=str, help="literal text to synthesize")
    src.add_argument("--text-file", type=Path, help="read text from file")
    ap.add_argument("--voice", choices=list(VOICES), help="voice (default: configured)")
    ap.add_argument("--out", type=Path, default=Path("out.mp3"))
    ap.add_argument("--list", action="store_true", help="list voices and exit")
    ap.add_argument("--set-default", metavar="VOICE", help="persist default voice")
    ap.add_argument("--get-default", action="store_true", help="print default voice and exit")
    args = ap.parse_args()

    if args.list:
        cur = get_default_voice()
        for k, v in VOICES.items():
            mark = "*" if k == cur else " "
            print(f" {mark} {k:8s}  {v['label']:25s}  {v['description']}")
        return

    if args.get_default:
        print(get_default_voice())
        return

    if args.set_default:
        set_default_voice(args.set_default)
        return

    if not args.text and not args.text_file:
        ap.error("need --text or --text-file (or --list / --set-default / --get-default)")

    text = args.text if args.text else args.text_file.read_text(encoding="utf-8")
    text = text.strip()
    if not text:
        sys.exit("empty input text")

    voice = args.voice or get_default_voice()
    synthesize(text, voice, args.out)


if __name__ == "__main__":
    main()
