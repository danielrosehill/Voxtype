"""
Modal app: Chatterbox TTS for Corn & Herman voices.

Reuses the public voice conditionals hosted by My-Weird-Prompts (the same
voices used in the podcast), so we don't have to ship the .pt files in this
repo or recompute them.

Deploy once:    modal deploy tts/modal_tts.py
Invoke ad-hoc: modal run tts/modal_tts.py::synthesize --text "Hello there" --voice corn

The CLI in `tts/synthesize.py` calls this remotely via `TTS.synthesize.remote()`.
"""

from __future__ import annotations

import modal

VOICE_CONDS_URLS = {
    "corn":   "https://ai-files.myweirdprompts.com/voices/corn/corn_conds.pt",
    "herman": "https://ai-files.myweirdprompts.com/voices/herman/herman_conds.pt",
}

app = modal.App("noisy-voice-tts")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "git")
    .pip_install(
        "torch>=2.0.0",
        "torchaudio>=2.0.0",
        "chatterbox-tts>=0.1.6",
        "requests",
    )
)

cache_volume = modal.Volume.from_name("noisy-voice-tts-cache", create_if_missing=True)


def _chunk_text(text: str, max_chars: int = 240) -> list[str]:
    """Split text on sentence/clause boundaries so each chunk fits Chatterbox's
    practical limit (~250 chars). Mirrors My-Weird-Prompts' chunker."""
    import re
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > max_chars:
            chunks.append(cur.strip())
            cur = s
        else:
            cur = (cur + " " + s).strip() if cur else s
    if cur:
        chunks.append(cur.strip())
    final: list[str] = []
    for c in chunks:
        if len(c) > max_chars * 1.5:
            for p in re.split(r"(?<=[,;])\s+", c):
                if final and len(final[-1]) + len(p) + 1 <= max_chars:
                    final[-1] = (final[-1] + " " + p).strip()
                else:
                    final.append(p.strip())
        else:
            final.append(c)
    return [c for c in final if c]


@app.cls(
    image=image,
    gpu="T4",
    timeout=1800,
    scaledown_window=300,
    volumes={"/cache": cache_volume},
)
class TTS:
    @modal.enter()
    def setup(self):
        import requests
        from pathlib import Path
        from chatterbox.tts import ChatterboxTTS, Conditionals
        import torch

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[TTS] loading ChatterboxTTS on {self.device}...")
        self.model = ChatterboxTTS.from_pretrained(device=self.device)
        self.Conditionals = Conditionals
        self.conds_cache: dict = {}

        cache_dir = Path("/cache/voice-conditionals")
        cache_dir.mkdir(parents=True, exist_ok=True)
        for voice, url in VOICE_CONDS_URLS.items():
            p = cache_dir / f"{voice}_conds.pt"
            if not p.exists():
                print(f"[TTS] downloading {voice} conditionals...")
                r = requests.get(url, timeout=120)
                r.raise_for_status()
                p.write_bytes(r.content)
            self.conds_cache[voice] = Conditionals.load(p, map_location=self.device)
        cache_volume.commit()
        print(f"[TTS] ready: {list(self.conds_cache.keys())}")

    @modal.method()
    def synthesize(self, text: str, voice: str) -> bytes:
        """Generate speech for `text` in the chosen voice; return MP3 bytes."""
        import subprocess
        import tempfile
        from pathlib import Path
        import torch
        import torchaudio

        if voice not in self.conds_cache:
            raise ValueError(f"unknown voice {voice!r}; available: {list(self.conds_cache)}")
        self.model.conds = self.conds_cache[voice]

        chunks = _chunk_text(text)
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            wav_parts = []
            for i, chunk in enumerate(chunks):
                print(f"[TTS] chunk {i+1}/{len(chunks)} ({len(chunk)}c) [{voice}]")
                wav = self.model.generate(chunk)
                wp = tdp / f"part_{i:04d}.wav"
                torchaudio.save(str(wp), wav.cpu(), self.model.sr)
                wav_parts.append(wp)

            if len(wav_parts) == 1:
                src = wav_parts[0]
            else:
                listf = tdp / "concat.txt"
                listf.write_text("\n".join(f"file '{p}'" for p in wav_parts))
                src = tdp / "joined.wav"
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf),
                     "-c", "copy", str(src)],
                    check=True, capture_output=True,
                )

            mp3 = tdp / "out.mp3"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-codec:a", "libmp3lame",
                 "-b:a", "192k", str(mp3)],
                check=True, capture_output=True,
            )
            return mp3.read_bytes()


@app.local_entrypoint()
def synthesize(text: str = "Hello there.", voice: str = "corn", out: str = "out.mp3"):
    """Local CLI shim: `modal run tts/modal_tts.py --text ... --voice ...`"""
    from pathlib import Path
    data = TTS().synthesize.remote(text, voice)
    Path(out).write_bytes(data)
    print(f"wrote {out} ({len(data)} bytes)")
