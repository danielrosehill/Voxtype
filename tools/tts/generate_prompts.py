#!/usr/bin/env python3
"""
Generate the canned Voxtype UI prompts directly into Voxtype's TTS asset
layout, in 16 kHz mono PCM WAV (matching the existing assets).

Default voice (`ryan`) writes to ``app/assets/tts/<key>.wav``.
Other voices write to ``app/assets/tts/voices/<voice>/<key>.wav`` so the
runtime voice-pack lookup can find them.

Usage (from the repo root):
  python -m tools.tts.generate_prompts                 # all voices
  python -m tools.tts.generate_prompts --voice ryan    # only ryan
  python -m tools.tts.generate_prompts --voice corn herman
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from .synthesize import synthesize
from .voices import VOICES

PROMPTS = {
    # Recording lifecycle
    "recording":            "Recording.",
    "stopped":              "Stopped.",
    "paused":               "Paused.",
    "resumed":              "Resumed.",
    "discarded":            "Discarded.",
    "cached":               "Cached.",
    "cleared":              "Cleared.",
    # Transcription pipeline
    "audio_sent":           "Audio sent.",
    "audio_sent_waiting":   "Audio sent. Waiting for transcription.",
    "transcribing":         "Transcribing.",
    "complete":             "Complete.",
    "error":                "Error.",
    # Output channels
    "clipboard":            "Clipboard.",
    "copied_to_clipboard":  "Copied to clipboard.",
    # Format / tone tweaks
    "format_updated":       "Format updated.",
    "tone_updated":         "Tone updated.",
    # Append flow
    "appending":            "Appending.",
    "appended":             "Appended.",
    # Toggles
    "tts_activated":        "Voice announcements on.",
    "tts_deactivated":      "Voice announcements off.",
    "vad_enabled":          "Voice activity detection on.",
    "vad_disabled":         "Voice activity detection off.",
    "app_enabled":          "App enabled.",
    "app_disabled":         "App disabled.",
    "clipboard_enabled":    "Clipboard output on.",
    "clipboard_disabled":   "Clipboard output off.",
    "inject_enabled":       "Inject on.",
    "inject_disabled":      "Inject off.",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS_TTS = REPO_ROOT / "app" / "assets" / "tts"


def _wav_target(voice: str, key: str, assets_dir: Path) -> Path:
    if voice == "ryan":
        return assets_dir / f"{key}.wav"
    return assets_dir / "voices" / voice / f"{key}.wav"


def _to_voxtype_wav(src_mp3: Path, dst_wav: Path) -> None:
    """Re-encode to 16 kHz mono PCM s16le, the format Voxtype's WAV cache
    expects."""
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src_mp3),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst_wav)],
        check=True, capture_output=True,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", nargs="+", choices=list(VOICES), default=list(VOICES))
    ap.add_argument("--assets-dir", type=Path, default=ASSETS_TTS,
                    help="Voxtype TTS assets dir (default: %(default)s)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for voice in args.voice:
            for key, text in PROMPTS.items():
                print(f"\n== {voice} :: {key} ==")
                tmp_mp3 = tdp / f"{key}.{voice}.mp3"
                try:
                    synthesize(text, voice, tmp_mp3)
                except Exception as e:
                    print(f"  FAILED synth ({voice} {key}): {e}")
                    continue
                dst = _wav_target(voice, key, args.assets_dir)
                try:
                    _to_voxtype_wav(tmp_mp3, dst)
                    print(f"  -> {dst}")
                except subprocess.CalledProcessError as e:
                    print(f"  FAILED ffmpeg ({voice} {key}): {e.stderr.decode()[:200]}")


if __name__ == "__main__":
    main()
