"""Download just the chosen segments and render them as ready-to-post clips."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import hinglish
from picker import Clip
from sources import YTDLP, Word, run

FONT_CAPTION = "caption"        # font roles, resolved per platform by platform_util.font()
FONT_HOOK = "hook"
SPEED = 1.0                # set by the CLI (--speed): play every clip faster, voice pitch kept
HOOK_INTRO = False          # set by the CLI (--hook-intro): open with a teaser + hook line
CAPTION_LANG = "hinglish"   # original | hinglish | english — set by the CLI (--captions)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "clip"


def download_section(url: str, start: float, end: float, out: Path) -> Path:
    if out.exists():
        return out
    run(YTDLP + ["-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                 "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
                 "--merge-output-format", "mp4", "-o", str(out), url])
    return out


# ---------- caption images ----------

def _font(role: str, size: int, text: str = "") -> ImageFont.FreeTypeFont:
    import platform_util
    if DEVANAGARI.search(text):  # Hindi captions need a font with Devanagari glyphs
        role = "devanagari"
    found = platform_util.font(role)
    try:
        if found:
            return ImageFont.truetype(found[0], size, index=found[1])
    except OSError:
        pass
    return ImageFont.load_default(size)


def _caption_png(text: str, width: int, path: Path, size: int) -> None:
    img = Image.new("RGBA", (width, int(size * 2.6)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(FONT_CAPTION, size, text)
    while d.textlength(text, font=font) > width - 100 and size > 30:
        size -= 4
        font = _font(FONT_CAPTION, size, text)
    d.text((width / 2, img.height / 2), text, font=font, anchor="mm", fill="white",
           stroke_width=max(4, size // 11), stroke_fill="black")
    img.save(path)


EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")


def _hook_png(text: str, width: int, path: Path) -> None:
    text = EMOJI.sub("", text).strip()   # caption fonts have no emoji glyphs (they'd render as boxes)
    font = _font(FONT_HOOK, 64, text)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if probe.textlength(trial, font=font) > width - 200 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    lines = lines[:3]
    line_h = 78
    box_w = int(max(probe.textlength(l, font=font) for l in lines)) + 80
    box_h = line_h * len(lines) + 50
    img = Image.new("RGBA", (width, box_h + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0 = (width - box_w) // 2
    d.rounded_rectangle([x0, 10, x0 + box_w, 10 + box_h], radius=24, fill="white")
    for i, l in enumerate(lines):
        d.text((width / 2, 10 + 25 + line_h * i + line_h / 2), l, font=font, anchor="mm", fill="black")
    img.save(path)


def _chunks(words: list[Word], start: float, end: float) -> list[tuple[float, float, str]]:
    """Group words into short 1-3 word captions (the fast-paced Shorts style)."""
    ws = [w for w in words if start <= w.t < end]
    groups: list[list[Word]] = []
    for w in ws:
        g = groups[-1] if groups else None
        if (g is None or len(g) >= 3 or w.t - g[-1].t > 0.6
                or len(" ".join(x.text for x in g + [w])) > 18):
            groups.append([w])
        else:
            g.append(w)
    out = []
    for i, g in enumerate(groups):
        t0 = g[0].t - start
        nxt = groups[i + 1][0].t - start if i + 1 < len(groups) else end - start
        t1 = min(nxt, g[-1].t - start + 1.2)
        text = re.sub(r"\[.*?\]", "", " ".join(w.text for w in g)).strip().upper()
        if text and t1 > t0:
            out.append((t0, t1, text))
    return out


NON_LATIN = re.compile(r"[^\x00-\u024F\u2000-\u206F\s]")


def caption_words(words: list[Word], end: float) -> list[Word]:
    """Words in the chosen caption language, keeping the original timing.
    original: as spoken (Devanagari for Hindi speech, English for English) · hinglish: Hindi in Roman
    script (English speech stays English) · english: translated phrase by phrase, the English words spread
    over each phrase's time span (English speech is used as-is)."""
    if CAPTION_LANG == "original" or not words:
        return words
    if CAPTION_LANG == "english":
        text = " ".join(w.text for w in words)
        if not NON_LATIN.search(text) and not _looks_hinglish(text):
            return words                       # already English: nothing to translate
        phrases: list[list[Word]] = []
        for w in words:
            if not phrases or w.t - phrases[-1][-1].t > 0.8 or len(phrases[-1]) >= 10:
                phrases.append([w])
            else:
                phrases[-1].append(w)
        tr = hinglish.translate_phrases([" ".join(w.text for w in ph) for ph in phrases])
        if tr:
            out: list[Word] = []
            for i, (ph, en) in enumerate(zip(phrases, tr)):
                toks = en.split()
                if not toks:
                    continue
                t0 = ph[0].t
                t1 = phrases[i + 1][0].t if i + 1 < len(phrases) else min(end, max(ph[-1].t + 1.0, t0 + 0.4 * len(toks)))
                step = max(t1 - t0, 0.2) / len(toks)
                out += [Word(t0 + k * step, tok) for k, tok in enumerate(toks)]
            return out
    return hinglish.to_hinglish(words)


HINGLISH_HINTS = re.compile(r"\b(hai|nahi|kya|bhai|kar|raha|rahe|tha|hum|tum|mera|kaise|yaar|accha|chalo|dekho)\b", re.I)


def _looks_hinglish(text: str) -> bool:
    return len(HINGLISH_HINTS.findall(text)) >= 3


def _caption_track(words: list[Word], clip: Clip, width: int, size: int, work: Path) -> Path | None:
    chunks = _chunks(words, clip.start, clip.end)
    if not chunks:
        return None
    work.mkdir(parents=True, exist_ok=True)
    blank = work / "blank.png"
    _caption_png("", width, blank, size)
    entries, cursor = [], 0.0
    for i, (t0, t1, text) in enumerate(chunks):
        if t0 > cursor + 0.01:
            entries.append((blank, t0 - cursor))
        p = work / f"cap_{i:04d}.png"
        _caption_png(text, width, p, size)
        entries.append((p, t1 - max(t0, cursor)))
        cursor = t1
    entries.append((blank, max(0.1, clip.end - clip.start - cursor)))
    lst = work / "captions.txt"
    lines = []
    for p, dur in entries:
        lines += [f"file '{p.resolve()}'", f"duration {max(dur, 0.04):.3f}"]
    lines.append(f"file '{entries[-1][0].resolve()}'")
    lst.write_text("\n".join(lines) + "\n")
    return lst


# ---------- final render ----------

def render(url: str, clip: Clip, words: list[Word], idx: int, out_dir: Path, work: Path,
           vertical: bool = True, captions: bool = True) -> Path:
    """VOD path: download just this clip's section, then render it."""
    raw = download_section(url, clip.start, clip.end, work / f"raw_{int(clip.start)}_{int(clip.end)}.mp4")
    out = out_dir / f"clip_{idx:02d}_{slug(clip.title)}.mp4"
    # the downloaded section starts at clip.start on the stream timeline
    return render_file(raw, clip.start, clip, words, out, work / f"cap_{idx:02d}", vertical, captions)


def _duration(path: Path) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


def render_file(src: Path, src_offset: float, clip: Clip, words: list[Word], out: Path,
                cw: Path, vertical: bool = True, captions: bool = True) -> Path:
    """Render `clip` from a local file. clip/words times are on a timeline where `src` begins at
    `src_offset` seconds (0 for a pre-cut section; the window start for a live buffer)."""
    length = clip.end - clip.start
    W, H = (1080, 1920) if vertical else (1920, 1080)
    cap_size = 88 if vertical else 72
    if captions:
        words = caption_words([w for w in words if clip.start <= w.t < clip.end], clip.end)
    track = _caption_track(words, clip, W, cap_size, cw) if captions else None

    seek = max(0.0, clip.start - src_offset)
    src_len = _duration(src)
    if src_len and seek > src_len - 1:
        raise ValueError(f"clip starts at {seek:.1f}s but the source is only {src_len:.1f}s long")
    inputs = ["-ss", f"{seek:.3f}", "-i", str(src)]
    if vertical:
        f = ("[0:v]split=2[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40[bg];"
             f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];")
        hook = cw / "hook.png"
        cw.mkdir(parents=True, exist_ok=True)
        _hook_png(clip.hook, W, hook)
        inputs += ["-loop", "1", "-i", str(hook)]
        f += "[v0][1:v]overlay=0:330:shortest=1[v1];"
        cap_y, cap_in = 1290, 2
    else:
        f = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2[v1];")
        cap_y, cap_in = H - int(cap_size * 2.6) - 40, 1
    if track:
        inputs += ["-f", "concat", "-safe", "0", "-i", str(track)]
        f += f"[{cap_in}:v]format=rgba[cap];[v1][cap]overlay=0:{cap_y}:eof_action=pass[v2];"
    else:
        f += "[v1]null[v2];"
    fade = 0.4
    f += f"[v2]fade=t=out:st={max(0.0, length - fade):.2f}:d={fade}[v]"

    cmd =["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", f, "-map", "[v]", "-map", "0:a?",
           "-af", f"afade=t=out:st={max(0.0, length - fade):.2f}:d={fade}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           "-t", f"{length:.2f}", str(out)]
    # a render never takes anywhere near this long; the timeout stops a bad input hanging a job forever
    subprocess.run(cmd, check=True, timeout=max(180, length * 6))
    if HOOK_INTRO:
        import hook
        hooked = hook.add_hook_intro(out, meta={"title": clip.title, "hook": clip.hook, "why": clip.why},
                                     model=hinglish.MODEL)
        hooked.replace(out)          # the hooked version becomes the clip
    if abs(SPEED - 1.0) > 1e-3:
        import speed
        fast = speed.speedup(out, SPEED)
        fast.replace(out)            # the sped-up version becomes the clip
    return out
