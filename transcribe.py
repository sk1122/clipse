"""Local speech-to-text with word timestamps (used in live mode, where YouTube has no captions yet)."""
from __future__ import annotations

from pathlib import Path

from sources import Word

_models: dict[str, object] = {}
DEVICE = "auto"   # set by the CLI: auto | cpu | mps | cuda


def transcribe(media: Path, model_size: str = "small", language: str | None = None) -> list[Word]:
    """Return words with times in seconds from the start of `media`. Empty list if whisper isn't installed."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return []
    if model_size not in _models:
        print(f"    loading whisper '{model_size}' (first run downloads the model)...")
        _models[model_size] = _load(WhisperModel, model_size)
    segments, _ = _models[model_size].transcribe(
        _load_audio(media), word_timestamps=True, vad_filter=True, language=language)
    return [Word(w.start, w.word.strip()) for s in segments for w in (s.words or []) if w.word.strip()]


def _load_audio(media: Path):
    """Decode to 16 kHz mono float32 with ffmpeg ourselves (sidesteps PyAV version mismatches)."""
    import subprocess

    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(media), "-vn",
                          "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0


def _load(WhisperModel, name: str):
    """GPU when available (Apple 'mps' on macOS, NVIDIA 'cuda' elsewhere), else CPU int8."""
    import sys
    gpus = {"auto": ["mps"] if sys.platform == "darwin" else ["cuda"], "mps": ["mps"], "cuda": ["cuda"],
            "cpu": []}[DEVICE]
    for dev in gpus:
        try:
            return WhisperModel(name, device=dev, compute_type="float16")
        except Exception:
            if DEVICE == dev:
                raise
    return WhisperModel(name, device="cpu", compute_type="int8")
