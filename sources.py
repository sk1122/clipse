"""Fetch everything we need from YouTube via yt-dlp: metadata, chat replay, captions, audio."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

YTDLP = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"]


@dataclass
class Word:
    t: float  # seconds from video start
    text: str


@dataclass
class ChatMsg:
    t: float
    text: str


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def fetch_info(url: str, work: Path) -> dict:
    cache = work / "info.json"
    if cache.exists():
        return json.loads(cache.read_text())
    info = json.loads(run(YTDLP + ["-J", url]).stdout)
    cache.write_text(json.dumps(info))
    return info


# ---------- live chat replay ----------

def _chat_text(item: dict) -> str | None:
    for key in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
        r = item.get(key)
        if r:
            runs = r.get("message", {}).get("runs", [])
            parts = [x.get("text") or x.get("emoji", {}).get("shortcuts", [""])[0] for x in runs]
            return "".join(p for p in parts if p)
    return None


def parse_live_chat_line(line: str) -> list[ChatMsg]:
    """One line of a live (still-running) chat dump → messages stamped with wall-clock epoch seconds."""
    try:
        actions = json.loads(line)["replayChatItemAction"]["actions"]
    except (KeyError, ValueError, TypeError):
        return []
    out = []
    for a in actions:
        item = a.get("addChatItemAction", {}).get("item", {})
        text = _chat_text(item)
        r = next(iter(item.values()), {}) if item else {}
        if text is not None and r.get("timestampUsec"):
            out.append(ChatMsg(int(r["timestampUsec"]) / 1e6, text))
    return out


def fetch_chat(url: str, info: dict, work: Path) -> list[ChatMsg]:
    if "live_chat" not in (info.get("subtitles") or {}):
        return []
    path = work / f"{info['id']}.live_chat.json"
    if not path.exists():
        print("  downloading chat replay (can take a while for long streams)...")
        try:
            run(YTDLP + ["--skip-download", "--write-subs", "--sub-langs", "live_chat",
                         "-o", str(work / "%(id)s"), url])
        except subprocess.CalledProcessError as e:
            print(f"  chat replay unavailable: {e.stderr.strip()[:200]}")
            return []
    msgs: list[ChatMsg] = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            action = json.loads(line)["replayChatItemAction"]
            t = int(action["videoOffsetTimeMsec"]) / 1000
            for a in action.get("actions", []):
                item = a.get("addChatItemAction", {}).get("item", {})
                text = _chat_text(item)
                if text is not None:
                    msgs.append(ChatMsg(t, text))
        except (KeyError, ValueError, TypeError):
            continue
    return msgs


# ---------- captions (word-level timestamps) ----------

def _pick_caption_lang(info: dict) -> tuple[str, bool] | None:
    """Return (lang, is_auto). Prefer human subs, then auto subs in the stream's own language."""
    lang = (info.get("language") or "en").split("-")[0]
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    for k in manual:
        if k.split("-")[0] == lang:
            return k, False
    auto = info.get("automatic_captions") or {}
    for k in (f"{lang}-orig", lang, "en-orig", "en"):
        if k in auto:
            return k, True
    origs = [k for k in auto if k.endswith("-orig")]
    if origs:
        return origs[0], True
    return None


def fetch_words(url: str, info: dict, work: Path) -> list[Word]:
    pick = _pick_caption_lang(info)
    if not pick:
        return []
    lang, is_auto = pick
    path = work / f"{info['id']}.{lang}.json3"
    if not path.exists():
        flag = "--write-auto-subs" if is_auto else "--write-subs"
        try:
            run(YTDLP + ["--skip-download", flag, "--sub-langs", lang, "--sub-format", "json3",
                         "-o", str(work / "%(id)s"), url])
        except subprocess.CalledProcessError as e:
            print(f"  captions unavailable: {e.stderr.strip()[:200]}")
            return []
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    words: list[Word] = []
    events = [e for e in data.get("events", []) if e.get("segs")]
    for i, ev in enumerate(events):
        base = ev.get("tStartMs", 0)
        dur = ev.get("dDurationMs") or (events[i + 1]["tStartMs"] - base if i + 1 < len(events) else 2000)
        segs = [s for s in ev["segs"] if s.get("utf8", "").strip()]
        for j, seg in enumerate(segs):
            start = base + seg.get("tOffsetMs", 0)
            nxt = base + segs[j + 1].get("tOffsetMs", 0) if j + 1 < len(segs) else base + dur
            toks = seg["utf8"].split()
            # human subs put a whole line in one seg: spread its words across the seg's span
            step = max(nxt - start, 1) / max(len(toks), 1)
            for k, tok in enumerate(toks):
                words.append(Word((start + k * step) / 1000, tok))
    words.sort(key=lambda w: w.t)
    return words


# ---------- audio loudness ----------

def fetch_loudness(url: str, info: dict, work: Path) -> list[tuple[float, float]]:
    """Momentary loudness (LUFS) every 100ms, via ffmpeg's ebur128 scanner."""
    cache = work / "loudness.json"
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text())]
    existing = list(work.glob("audio.*"))
    if not existing:
        print("  downloading audio track...")
        run(YTDLP + ["-f", "worstaudio/bestaudio", "-o", str(work / "audio.%(ext)s"), url])
        existing = list(work.glob("audio.*"))
    print("  measuring loudness...")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-v", "verbose", "-i", str(existing[0]), "-vn",
         "-af", "aresample=16000,ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    out: list[tuple[float, float]] = []
    for line in proc.stderr.splitlines():
        if " t: " in line and " M: " in line:
            try:
                t = float(line.split(" t: ")[1].split()[0])
                m = float(line.split(" M: ")[1].split()[0])
                out.append((t, m))
            except (IndexError, ValueError):
                continue
    cache.write_text(json.dumps(out))
    return out
