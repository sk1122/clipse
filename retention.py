"""Edits that keep people watching (all optional, all in place on a finished clip):

  jump_cuts   trims dead air: stretches where nobody talks and nothing happens shrink to a beat
  add_sfx     a whoosh as the clip starts and a deep boom on the punchline (the loudest moment).
              Both are synthesised here from noise and sine waves, so there's nothing to license
  loop_end    the last 0.3 s fades into the clip's first frame, so a replay looks seamless
  cover_time  the moment to use as the Reel's cover: the biggest reaction, not the first frame
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 48000


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _loudness(path: Path) -> list[tuple[float, float]]:
    import catchup
    # ebur128's momentary loudness covers the 0.4 s before each timestamp; centre it
    return [(max(0.0, t - 0.2), m) for t, m in catchup.loudness(path)]


def _ffmpeg(*args: str, timeout: int = 600) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, timeout=timeout)


# ---------------------------------------------------------------- jump cuts

def dead_air(path: Path, min_gap: float = 0.7, keep: float = 0.15, max_share: float = 0.3) -> list[tuple[float, float]]:
    """Stretches to cut: quiet for at least `min_gap` seconds (relative to this clip's own loudness, so game
    sound under the voice doesn't count as quiet). A `keep`-second beat is left on each side; at most
    `max_share` of the clip is removed, longest gaps first."""
    loud = _loudness(path)
    length = _duration(path)
    if len(loud) < 20 or length < 8:
        return []
    levels = np.array([m for _, m in loud])
    quiet = max(-50.0, float(np.median(levels)) - 14.0)
    runs, start = [], None
    for t, m in loud:
        if m < quiet and start is None:
            start = t
        elif m >= quiet and start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, length))
    cuts = [(a + keep, b - keep) for a, b in runs
            if b - a >= min_gap and a > 0.5 and b < length - 0.5]   # never touch the very start or end
    cuts.sort(key=lambda c: c[1] - c[0], reverse=True)
    out, total = [], 0.0
    for a, b in cuts:
        if total + (b - a) > max_share * length:
            continue
        out.append((a, b))
        total += b - a
    return sorted(out)


def jump_cuts(path: Path, **kw) -> float:
    """Cut dead air from the clip (in place). Returns the seconds removed."""
    cuts = dead_air(path, **kw)
    if not cuts:
        return 0.0
    drop = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in cuts)
    tmp = path.with_suffix(".jc.mp4")
    _ffmpeg("-i", str(path),
            "-vf", f"select='not({drop})',setpts=N/FRAME_RATE/TB",
            "-af", f"aselect='not({drop})',asetpts=N/SR/TB",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp))
    tmp.replace(path)
    return sum(b - a for a, b in cuts)


# ---------------------------------------------------------------- sound effects

def _bandsweep(n: int, f0: float, f1: float, f2: float, rng) -> np.ndarray:
    """Noise whose brightness sweeps f0 → f1 → f2 (a whoosh), built from overlapping filtered frames."""
    noise = rng.standard_normal(n + 2048)
    out = np.zeros(n + 2048)
    hop, win = 512, np.hanning(1024)
    freqs = np.fft.rfftfreq(1024, 1 / SR)
    for i in range(0, n, hop):
        x = i / max(1, n)
        fc = f0 + (f1 - f0) * (x / 0.6) if x < 0.6 else f1 + (f2 - f1) * ((x - 0.6) / 0.4)
        X = np.fft.rfft(noise[i:i + 1024] * win)
        X *= 1 / np.sqrt(1 + (freqs / fc) ** 4) * (1 - 1 / np.sqrt(1 + (freqs / (fc * 0.25)) ** 4))
        out[i:i + 1024] += np.fft.irfft(X, 1024) * win
    return out[:n]


def whoosh(seconds: float = 0.45, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    x = _bandsweep(n, 400, 5000, 900, rng)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * t ** 0.7) ** 2                  # swells up, then away
    x *= env
    return x / (np.abs(x).max() + 1e-9)


def boom(seconds: float = 1.1, seed: int = 0) -> np.ndarray:
    """A deep cinematic impact: a sub-bass hit that drops in pitch, with a short punchy click on top."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    f = 38 + 50 * np.exp(-t / 0.08)                      # pitch falls fast from ~88 Hz to 38 Hz
    phase = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * np.exp(-t / 0.35)
    click = rng.standard_normal(n) * np.exp(-t / 0.006) * 0.5
    x = np.tanh(2.5 * (body + click))                    # saturate for weight on phone speakers
    x[: int(0.002 * SR)] *= np.linspace(0, 1, int(0.002 * SR))
    return x / (np.abs(x).max() + 1e-9)


def _write_wav(x: np.ndarray, path: Path) -> None:
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def punchline(path: Path, skip: float = 0.0) -> float | None:
    """The loudest moment after `skip` seconds (not in the last second)."""
    length = _duration(path)
    cands = [(m, t) for t, m in _loudness(path) if skip + 0.8 <= t <= length - 1.0]
    return max(cands)[1] if cands else None


def add_sfx(path: Path, skip: float = 0.0, volume: float = 0.55) -> tuple[float | None, float | None]:
    """Mix a whoosh where the clip proper starts (`skip`, after any hook intro) and a boom just before the
    punchline. Returns (whoosh time, boom time)."""
    length = _duration(path)
    if length < 3:
        return None, None
    seed = sum(map(ord, path.name))
    track = np.zeros(int((length + 1.5) * SR))
    w_at = max(0.0, skip - 0.2)
    w = whoosh(seed=seed) * 0.6
    i = int(w_at * SR)
    track[i:i + len(w)] += w[: len(track) - i]
    b_at = punchline(path, skip)
    if b_at is not None:
        b = boom(seed=seed)
        i = int(max(0.0, b_at - 0.05) * SR)
        track[i:i + len(b)] += b[: len(track) - i]
    wav = path.with_suffix(".sfx.wav")
    _write_wav(track[: int(length * SR)] * volume, wav)
    tmp = path.with_suffix(".sfxmix.mp4")
    has_audio = bool(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                     "stream=index", "-of", "csv=p=0", str(path)],
                                    capture_output=True, text=True).stdout.strip())
    graph = ("[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];"
             "[1:a]aresample=48000,aformat=channel_layouts=stereo[s];"
             "[a0][s]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]") if has_audio else \
        "[1:a]aformat=channel_layouts=stereo[a]"
    _ffmpeg("-i", str(path), "-i", str(wav), "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp))
    tmp.replace(path)
    wav.unlink(missing_ok=True)
    return w_at, b_at


# ---------------------------------------------------------------- loop ending

def loop_end(path: Path, fade: float = 0.3) -> None:
    """Fade the last `fade` seconds into the clip's own first frame, so the replay continues seamlessly."""
    length = _duration(path)
    if length < 4:
        return
    first = path.with_suffix(".first.png")
    _ffmpeg("-i", str(path), "-frames:v", "1", str(first))
    tmp = path.with_suffix(".loop.mp4")
    _ffmpeg("-i", str(path), "-loop", "1", "-t", f"{fade + 0.1:.2f}", "-i", str(first),
            "-filter_complex",
            f"[1:v]scale=1080:1920,setsar=1,fps=30,format=yuv420p[f];"
            f"[0:v]fps=30,format=yuv420p,setsar=1[m];"
            f"[m][f]xfade=transition=fade:duration={fade}:offset={length - fade:.3f},trim=0:{length:.3f}[v]",
            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", f"{length:.3f}", "-movflags", "+faststart", str(tmp))
    tmp.replace(path)
    first.unlink(missing_ok=True)


# ---------------------------------------------------------------- cover frame

def cover_time(path: Path, skip: float = 3.0) -> float:
    """Seconds into the clip for the cover: just after the biggest reaction (past any hook intro)."""
    length = _duration(path)
    t = punchline(path, min(skip, length / 3))
    return min(length - 0.5, (t if t is not None else length * 0.4) + 0.3)
