"""Voice registry shared by the synthesize CLI."""

from __future__ import annotations

VOICES = {
    "corn": {
        "label": "Corn",
        "backend": "chatterbox",
        "description": "Curious, playful co-host (Chatterbox, GPU via Modal)",
    },
    "herman": {
        "label": "Herman Poppleberry",
        "backend": "chatterbox",
        "description": "Research-driven co-host (Chatterbox, GPU via Modal)",
    },
    "ryan": {
        "label": "Ryan Neural",
        "backend": "edge",
        # Microsoft Edge TTS voice id
        # en-US-RyanNeural was retired by Microsoft; en-GB-RyanNeural is the
        # current Ryan voice on the Edge TTS service.
        "edge_voice_id": "en-GB-RyanNeural",
        "description": "Microsoft Edge neural TTS, en-GB Ryan (local, free, CPU)",
    },
}

DEFAULT_VOICE = "corn"
