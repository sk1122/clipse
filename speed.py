"""Speed clips up (e.g. 1.5x or 2x) without chipmunk voices: video is retimed, audio is time-stretched
with ffmpeg's atempo, which keeps the pitch."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _atempo(factor: float) -> str:
    """atempo is most accurate between 0.5 and 2.0, so larger factors are chained (e.g. 3x = 2.0 * 1.5)."""
    parts, f = [], factor
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def label(factor: float) -> str:
    return f"{factor:g}x"


def speedup(src: Path, factor: float, out: Path | None = None) -> Path:
    """Write a sped-up copy (clip_..._2x.mp4) next to the original and return it. factor 1 = no-op."""
    src = Path(src)
    if abs(factor - 1.0) < 1e-3:
        return src
    out = out or src.with_name(f"{src.stem}_{label(factor)}{src.suffix}")
    has_audio = bool(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                     "stream=index", "-of", "csv=p=0", str(src)],
                                    capture_output=True, text=True).stdout.strip())
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-filter:v", f"setpts=PTS/{factor},fps=30"]
    cmd += ["-filter:a", _atempo(factor)] if has_audio else ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, timeout=600)
    return out
