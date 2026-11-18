"""Hook intro: open a clip with its best 2.5 seconds (a teaser cold-open) and a big hook line, then a quick
flash and the full clip. Works on any finished clip:  clipse hook <clip.mp4> [--text "..."]"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

import catchup
import render

TEASER = 2.5      # seconds of payoff shown first
FLASH = 0.18      # white "rewind" flash between teaser and clip
W, H = 1080, 1920


def _duration(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(p)], capture_output=True, text=True).stdout)


def best_moment(clip: Path, length: float = TEASER) -> float:
    """Start of the loudest `length`-second stretch, skipping the first seconds (the setup)."""
    loud = catchup.loudness(clip)
    dur = _duration(clip)
    if not loud:
        return max(0.0, dur * 0.6)
    best, best_t = -1e9, max(0.0, dur * 0.6)
    lo = min(3.0, dur / 3)
    for t, _ in loud[::5]:                       # every 0.5 s
        if t < lo or t + length > dur - 0.3:
            continue
        vals = [m for tt, m in loud if t <= tt < t + length]
        if vals:
            score = sum(vals) / len(vals)
            if score > best:
                best, best_t = score, t
    return best_t


def opener_line(meta: dict, model: str | None) -> str:
    """A short cold-open line, written by the judge model when available."""
    if model and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            from pydantic import BaseModel

            class Line(BaseModel):
                line: str

            resp = anthropic.Anthropic().messages.parse(
                model=model, max_tokens=2000, output_config={"effort": "low"}, output_format=Line,
                messages=[{"role": "user", "content":
                    "Write the cold-open hook for a short-form clip: 2-6 words shown over the first 2 seconds, "
                    "which tease the payoff WITHOUT spoiling it (e.g. 'HE DID NOT SEE THIS COMING', 'WAIT FOR "
                    "THE END', 'CHAT WAS NOT READY'). ALL CAPS, at most one emoji, same language as the title "
                    "(Hinglish for Hindi titles). It must differ from the existing on-screen hook.\n\n"
                    f"Title: {meta.get('title')}\nExisting hook: {meta.get('hook')}\nWhy it works: {meta.get('why')}"}])
            if resp.parsed_output and resp.parsed_output.line.strip():
                return resp.parsed_output.line.strip()
        except Exception as e:
            print(f"    (hook line from the judge unavailable: {type(e).__name__}; using a default)")
    return "WAIT FOR IT 👀"


def _hook_card(text: str, path: Path) -> None:
    text = render.EMOJI.sub("", text).strip().upper()
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = 110
    f = render._font(render.FONT_CAPTION, size, text)
    words, lines = text.split(), []
    while True:
        lines, cur = [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if d.textlength(trial, font=f) > W - 140 and cur:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        if len(lines) <= 3 or size <= 60:
            break
        size -= 10
        f = render._font(render.FONT_CAPTION, size, text)
    lh = int(size * 1.18)
    y = H // 2 - lh * len(lines) // 2
    for line in lines:
        tw = d.textlength(line, font=f)
        pad = 34
        d.rounded_rectangle([(W - tw) / 2 - pad, y - 12, (W + tw) / 2 + pad, y + lh - 4], radius=26,
                            fill=(255, 221, 0, 255))
        d.text((W / 2, y + lh / 2 - 6), line, font=f, anchor="mm", fill="black")
        y += lh + 10
    img.save(path)


def add_hook_intro(clip: Path, out: Path | None = None, text: str | None = None, meta: dict | None = None,
                   model: str | None = "claude-opus-5-5") -> Path:
    clip = Path(clip)
    out = out or clip.with_name(clip.stem + "_hooked.mp4")
    meta = meta or {}
    line = text or opener_line(meta, model)
    t = best_moment(clip)
    card = out.with_suffix(".hook.png")
    _hook_card(line, card)
    dur = _duration(clip)
    pop = 0.22   # the hook card "pops" in
    f = (
        # teaser: the payoff, slightly zoomed for energy, with the hook card popping in
        f"[0:v]split=2[v0][v1];[0:a]asplit=2[a0][a1];"
        f"[v0]trim={t:.2f}:{t + TEASER:.2f},setpts=PTS-STARTPTS,scale=iw*1.08:ih*1.08,crop={W}:{H},setsar=1,fps=30,format=yuv420p[tv];"
        f"[1:v]format=rgba,scale=w='{W}*min(1,0.85+0.15*t/{pop})':h=-1:eval=frame[card];"
        f"[tv][card]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,setsar=1,fps=30[teaser];"
        f"[a0]atrim={t:.2f}:{t + TEASER:.2f},asetpts=PTS-STARTPTS,afade=t=out:st={TEASER - 0.2:.2f}:d=0.2[ta];"
        # flash
        f"color=c=white:s={W}x{H}:r=30:d={FLASH},format=yuv420p,setsar=1[fl];"
        f"anullsrc=r=48000:cl=stereo,atrim=0:{FLASH}[fa];"
        # the full clip
        f"[v1]scale={W}:{H},setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[mv];"
        f"[a1]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[ma];"
        f"[ta]aresample=48000,aformat=channel_layouts=stereo[ta2];"
        f"[teaser][ta2][fl][fa][mv][ma]concat=n=3:v=1:a=1[v][a]"
    )
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(clip), "-loop", "1", "-t",
                    f"{TEASER}", "-i", str(card), "-filter_complex", f, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)],
                   check=True, timeout=max(180, dur * 6))
    card.unlink(missing_ok=True)
    print(f"    hook intro: \"{line}\" + teaser from {t:.1f}s → {out.name}")
    return out


def meta_for(clip: Path) -> dict:
    """Find a clip's title/hook/why in its job's events, if it was made by clipse."""
    for job in Path(clip).resolve().parents:
        ev = job / "events.jsonl"
        if ev.exists():
            for line in ev.read_text().splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("kind") == "clip" and e.get("file") and Path(e["file"]).name == Path(clip).name:
                    return e
            break
    return {}
