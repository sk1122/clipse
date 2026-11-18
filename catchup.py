"""Catch-up: clip what already happened on a live stream, from its very first second.

YouTube serves a live stream (with --live-from-start) as numbered 5-second chunks: chunk N covers
seconds [5N, 5N+5) of the stream. We download every chunk of the cheapest audio, find loud moments,
transcribe just those, let Claude pick, then fetch only the video chunks the chosen clips need.
Live chat isn't available for the past part, so this pass runs on audio + transcript.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import picker
import progress
import render
import signals
import sources
import transcribe

CHUNK = 5.0   # typical chunk length; the real one is read from YouTube per stream (often 2s or 5s)
UA = {"User-Agent": "Mozilla/5.0"}
WHISPER_LOCK = threading.Lock()   # live mode transcribes at the same time


def _formats(url: str) -> dict:
    info = json.loads(subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--no-warnings", "--live-from-start", "-J", url],
        capture_output=True, text=True, check=True).stdout)
    return {f["format_id"]: f for f in info["formats"]
            if f.get("protocol", "").startswith("http_dash_segments")}


def _pick(fmts: dict, kind: str, max_h: int) -> dict:
    if kind == "audio_low":
        cands = [f for f in fmts.values() if f.get("vcodec") == "none"]
        return min(cands, key=lambda f: f.get("abr") or f.get("tbr") or 1e9)
    if kind == "audio":
        cands = [f for f in fmts.values() if f.get("vcodec") == "none"]
        return max(cands, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    cands = [f for f in fmts.values() if f.get("vcodec") != "none" and (f.get("height") or 0) <= max_h
             and str(f.get("vcodec", "")).startswith("avc")]
    return max(cands, key=lambda f: ((f.get("height") or 0), f.get("fps") or 0))


def _get(url: str, sq: int, dest: Path, tries: int = 3) -> tuple[bool, dict]:
    if dest.exists() and dest.stat().st_size > 0:
        return True, {}
    for _ in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(f"{url}&sq={sq}", headers=UA), timeout=30)
            dest.write_bytes(r.read())
            return True, dict(r.headers)
        except Exception:
            continue
    return False, {}


def head_seq(fmt: dict, tmp: Path) -> tuple[int, float]:
    """(number of chunks so far, seconds per chunk). Chunk length differs between streams, so it's
    measured from YouTube's own headers rather than assumed."""
    ok, headers = _get(fmt["url"], 0, tmp / "probe.mp4")
    (tmp / "probe.mp4").unlink(missing_ok=True)
    if not ok or "X-Head-Seqnum" not in headers:
        raise RuntimeError("couldn't read the stream's chunk index")
    seq = int(headers["X-Head-Seqnum"])
    head_time = float(headers.get("X-Head-Time-Sec") or seq * CHUNK)
    return seq, (round(head_time / seq, 3) if seq else CHUNK)


def fetch_range(fmt: dict, first: int, last: int, folder: Path, workers: int = 8, on_progress=None) -> list[Path]:
    """Download chunks first..last. YouTube throttles bursts, so missing chunks get retried in
    later passes with growing pauses instead of being given up on."""
    import time
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / f"{sq:07d}.mp4" for sq in range(first, last + 1)]
    missing = paths
    for attempt in range(6):
        done_n = [len(paths) - len(missing)]
        def one(p):
            r = _get(fmt["url"], int(p.stem), p, tries=1)[0]
            done_n[0] += 1
            if on_progress and done_n[0] % 50 == 0:
                on_progress(done_n[0] / len(paths))
            return r
        with ThreadPoolExecutor(workers) as ex:
            ok = list(ex.map(one, missing))
        missing = [p for p, good in zip(missing, ok) if not good]
        if not missing:
            break
        time.sleep(3 * (attempt + 1))
    return [p for p in paths if p.exists() and p.stat().st_size > 0]


def _boxes(data: bytes):
    i = 0
    while i + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[i:i + 8])
        if n < 8:
            break
        yield typ, data[i:i + n]
        i += n


def join(parts: list[Path], out: Path) -> Path:
    """Stitch fragmented-MP4 chunks into one file at the box level: keep the first chunk's header,
    then every chunk's media fragments. Each fragment carries its true stream timestamp, so the
    result's timeline IS the stream timeline (an ffmpeg concat would renumber/scramble it)."""
    with open(out, "wb") as f:
        for k, p in enumerate(parts):
            for typ, box in _boxes(p.read_bytes()):
                if (k == 0 and typ in (b"ftyp", b"moov")) or typ in (b"moof", b"mdat"):
                    f.write(box)
    return out


def loudness(audio: Path) -> list[tuple[float, float]]:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-v", "verbose", "-i", str(audio), "-vn",
                           "-af", "aresample=16000,ebur128=framelog=verbose", "-f", "null", "-"],
                          capture_output=True, text=True)
    out = []
    for line in proc.stderr.splitlines():
        if " t: " in line and " M: " in line:
            try:
                out.append((float(line.split(" t: ")[1].split()[0]), float(line.split(" M: ")[1].split()[0])))
            except (IndexError, ValueError):
                pass
    return out


def render_clip(c, i: int, vid: dict, a_hi: dict, chunk: float, duration: float, work: Path, out: Path, a) -> Path:
    """Fetch just the chunks a clip needs, then caption it from its OWN audio, so the captions can
    never drift from the picture whatever the earlier analysis did."""
    first, last = int(max(0, c.start - 2) // chunk), int(min(duration - 1, c.end + 2) // chunk)
    v = join(fetch_range(vid, first, last, work / f"v{i}"), work / f"v{i}.mp4")
    au = join(fetch_range(a_hi, first, last, work / f"a{i}"), work / f"a{i}.m4a")
    raw = work / f"raw_{i}.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(v), "-i", str(au),
                    "-map", "0:v", "-map", "1:a", "-c", "copy", str(raw)], check=True)
    words = []
    if not a.no_captions:
        with WHISPER_LOCK:
            words = [sources.Word(w.t + first * chunk, w.text) for w in transcribe.transcribe(raw, a.speech_model)]
    return render.render_file(raw, first * chunk, c, words,
                              out / f"clip_{signals.fmt(c.start).replace(':', '')}_{render.slug(c.title)}.mp4",
                              work / f"cap_{i}", vertical=not a.landscape, captions=not a.no_captions)


def run(a, info: dict, out_dir: Path, log=print) -> int:
    """Clip the part of a live stream that already aired. Returns number of clips made."""
    work = out_dir / "from_start" / "work"
    work.mkdir(parents=True, exist_ok=True)
    fmts = _formats(a.url)
    a_low, a_hi, vid = _pick(fmts, "audio_low", 0), _pick(fmts, "audio", 0), _pick(fmts, "video", a.live_quality)
    head, chunk = head_seq(a_low, work)
    duration = head * chunk
    log(f"[from start] stream is {signals.fmt(duration)} long so far — downloading its audio "
        f"({head} chunks)...")
    S = lambda n, name, detail="", frac=None: progress.stage(n, 5, name, detail, part="from_start", frac=frac)
    S(1, "Downloading audio from the start", f"{signals.fmt(duration)} of stream")
    parts = fetch_range(a_low, 0, head - 1, work / "audio_chunks",
                        on_progress=lambda f: S(1, "Downloading audio from the start", f"{signals.fmt(duration)} of stream", f))
    if len(parts) < head * 0.9:
        log(f"[from start] only got {len(parts)}/{head} audio chunks — YouTube may not keep the full DVR. "
            "Analysing what we have.")
    audio = join(parts, work / "audio.m4a")
    for p in parts:
        p.unlink(missing_ok=True)

    log("[from start] scanning loudness...")
    S(2, "Scanning loudness")
    loud = loudness(audio)
    score, norm = signals.hype_curve(duration, [], loud, None)
    n_cands = min(14, max(a.clips * 3, 8))
    cands = signals.pick_candidates(score, norm, n_cands, min_gap=180.0, skip_intro=120.0)

    log(f"[from start] transcribing {len(cands)} loud moments...")
    words: list[sources.Word] = []
    for ci, c in enumerate(cands):
        S(3, "Transcribing loud moments", f"{ci + 1}/{len(cands)} at {signals.fmt(c.peak)}", ci / max(1, len(cands)))
        lo = max(0.0, c.peak - 90)
        seg = work / f"seg_{int(lo)}.wav"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{lo:.2f}", "-t", "135",
                        "-i", str(audio), "-ac", "1", "-ar", "16000", str(seg)], check=True)
        with WHISPER_LOCK:
            ws = transcribe.transcribe(seg, a.speech_model)
        words += [sources.Word(w.t + lo, w.text) for w in ws]
        seg.unlink(missing_ok=True)
    words.sort(key=lambda w: w.t)
    signals.attach_context(cands, words, [])

    S(4, "Choosing clips", "Claude is judging" if not a.no_ai else "signal-only picks")
    clips = None
    if not a.no_ai:
        try:
            clips = picker.choose_with_claude(cands, {**info, "duration": duration}, a.clips,
                                              a.min_len, a.max_len, a.judge)
        except Exception as e:
            log(f"[from start] Claude unavailable ({type(e).__name__}) — using signal-only picks")
    if not clips:
        clips = picker.choose_heuristic(cands, words, a.clips, a.min_len, a.max_len)
    picker.finish_all(clips, words, a.min_len, a.max_len, limit=duration)

    out = out_dir / "from_start"
    made = 0
    for i, c in enumerate(clips, 1):
        S(5, "Rendering", f"clip {i}/{len(clips)}", (i - 1) / max(1, len(clips)))
        try:
            path = render_clip(c, i, vid, a_hi, chunk, duration, work, out, a)
            with open(out / "clips.jsonl", "a") as f:
                f.write(json.dumps({**asdict(c), "stream_time": signals.fmt(c.start), "file": str(path)},
                                   ensure_ascii=False) + "\n")
            made += 1
            progress.emit("clip", **asdict(c), stream_time=signals.fmt(c.start), file=str(path), part="from_start")
            log(f"    ✓ [from start {signals.fmt(c.start)}] [{c.virality}/10] {c.title}\n      {path}")
        except Exception as e:
            log(f"    ✗ [from start] clip {i} failed: {type(e).__name__}: {str(e)[:200]}")
    progress.emit("done", clips=made, part="from_start")
    log(f"[from start] done — {made} clips from the first {signals.fmt(duration)} → {out}")
    return made
