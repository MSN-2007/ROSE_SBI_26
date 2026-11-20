"""providers/tts_edgetts.py — Ultra-realistic human neural speech synthesis via Microsoft Edge TTS.

Zero cost, zero API keys required, studio-grade neural voices.
Streams audio and decodes through PyAV directly into 24 kHz mono PCM16.
"""
from __future__ import annotations

import asyncio
import io
import re
from importlib.util import find_spec
from typing import AsyncIterator

from . import audio
from .base import DEFAULT_CAPS, Field

VOICE_PRESETS = (
    # ── Female voices (ROSE default) ──────────────────────────────────────────
    ("en-US-AriaNeural",        "Aria — Warm, natural American female ★ ROSE default"),
    ("en-GB-SoniaNeural",       "Sonia — Confident, polished British female"),
    ("en-US-JennyNeural",       "Jenny — Expressive, friendly American female"),
    ("en-US-MichelleNeural",    "Michelle — Calm, articulate American female"),
    ("en-US-SaraNeural",        "Sara — Clear, professional American female"),
    ("en-IN-NeerjaNeural",      "Neerja — Natural Indian English female"),
    # ── Male voices ───────────────────────────────────────────────────────────
    ("en-GB-RyanNeural",        "Ryan — British, cultured & refined (male)"),
    ("en-US-ChristopherNeural", "Christopher — Natural, warm American male"),
    ("en-US-GuyNeural",         "Guy — Conversational American male"),
    ("en-US-EricNeural",        "Eric — Calm, composed American male"),
    ("en-IN-PrabhatNeural",     "Prabhat — Natural Indian English male"),
)

VOICE_CHOICES = tuple(v[0] for v in VOICE_PRESETS)
DEFAULT_VOICE = "en-US-AriaNeural"


def _decode_mp3_to_pcm24(data: bytes) -> bytes:
    if not data:
        return b""
    import av
    import numpy as np

    out = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=audio.SPEAKER_RATE)
        for frame in container.decode(audio=0):
            for res in resampler.resample(frame):
                out.append(res.to_ndarray().reshape(-1))
        for res in resampler.resample(None):
            out.append(res.to_ndarray().reshape(-1))
    if not out:
        return b""
    return np.concatenate(out).astype("<i2").tobytes()


class EdgeTTSProvider:
    def __init__(self, cfg: dict):
        self.voice = (cfg.get("voice") or DEFAULT_VOICE).strip()
        self.rate = (cfg.get("rate") or "+0%").strip()
        self.pitch = (cfg.get("pitch") or "+0Hz").strip()
        self.caps = dict(DEFAULT_CAPS)
        self.caps["audio_out"] = True

    async def speak(self, text: str, voice: str = "") -> AsyncIterator[bytes]:
        import edge_tts

        v = (voice or self.voice or DEFAULT_VOICE).strip()
        text = (text or "").strip()
        if not text:
            return

        rate = self.rate if self.rate.startswith(("+", "-")) else f"+{self.rate}"
        pitch = self.pitch if self.pitch.startswith(("+", "-")) else f"+{self.pitch}"

        comm = edge_tts.Communicate(text, voice=v, rate=rate, pitch=pitch)
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])

        if buf:
            pcm = await asyncio.to_thread(_decode_mp3_to_pcm24, bytes(buf))
            # Yield in ~20ms chunks (960 bytes at 24kHz 16-bit mono)
            chunk_size = 960 * 4
            for i in range(0, len(pcm), chunk_size):
                yield pcm[i : i + chunk_size]


def _make(cfg: dict) -> EdgeTTSProvider:
    return EdgeTTSProvider(cfg)


PROVIDER = {
    "id": "edgetts",
    "kind": "tts",
    "label": "Microsoft Edge Neural",
    "hint": "Studio-grade, human-like neural voices — 100% free, no API key required",
    "requires": ("edge_tts", "av"),
    "catalog": (
        {"id": "edgetts", "label": "Microsoft Edge Neural Voice",
         "note": "free cloud neural · realistic human voices", "local": False,
         "values": {"voice": DEFAULT_VOICE}},
    ),
    "needs": (
        Field("voice", "Voice", kind="choice", choices=VOICE_CHOICES,
              default=DEFAULT_VOICE,
              hint="Aria (US Female ★ default), Sonia (British Female), Jenny, Michelle, Sara, Ryan (British Male)"),
        Field("rate", "Speed adjustment", kind="text", default="+0%",
              required=False, advanced=True, hint="e.g. +0%, +10%, -5%"),
        Field("pitch", "Pitch adjustment", kind="text", default="+0Hz",
              required=False, advanced=True, hint="e.g. +0Hz, +2Hz, -2Hz"),
    ),
    "caps": {"audio_out": True},
    "factory": _make,
}


def available() -> bool:
    return find_spec("edge_tts") is not None and find_spec("av") is not None
