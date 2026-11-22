"""Edit styles for vertical clips. Each builds an ffmpeg filter graph from the source video plus the webcam
box from facecam.py, so they work for any streamer's layout.

  classic     16:9 video on a blurred background, hook banner on top                       (always works)
  split       content on top, the streamer's webcam big underneath, title across the middle
  fullscreen  content fills the frame; a webcam bubble in a corner; title at the top
  reaction    the webcam fills most of the frame, the content in a strip below
  auto        split for a webcam overlay, fullscreen when the camera is the whole picture
              (IRL / just chatting / vlogs), classic when there's no camera

Layouts that need a webcam fall back to classic when none is found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

STYLES = ["auto", "classic", "split", "fullscreen", "reaction"]
W, H = 1080, 1920
STOP = set("a an the is are was were be to of in on at for and or but it this that i you he she we they my "
           "your his her our their me him us them with from by as so just got get gets do did does not no "
           "ka ki ke ko se hai ho ye wo bhi aur mein me game games gaming bro bhai chat stream live guys today "
           "really very what when where how why this that just like there here everyone everything "
           "something nothing all again out up about than then".split())
ACCENT = (255, 45, 45)          # highlight colour for the title's key word


@dataclass
class Layout:
    filter: str          # ffmpeg filter graph from [0:v] (+ [1:v] title image) to [v1]
    title_png: bool      # needs the title image as input 1
    caption_y: int       # where captions sit
    name: str


def resolve(style: str, cam) -> str:
    """The style actually used for this clip, given the webcam found (or not)."""
    if style == "auto":
        if cam is None:
            return "fullscreen"               # a letterboxed strip loses viewers; fill the screen
        return "fullscreen" if cam.full_frame else "split"
    if style in ("split", "reaction") and cam is not None and cam.full_frame:
        return "fullscreen"                   # the camera IS the picture: nothing to split
    if style in ("split", "reaction") and cam is None:
        return "fullscreen"
    return style


def keyword(text: str) -> str | None:
    """The word to highlight: the longest word that isn't filler (e.g. 'WWE' in 'they got WWE in the game')."""
    words = [w for w in re.findall(r"[A-Za-z0-9']+", text) if w.lower() not in STOP and len(w) >= 3]
    if not words:
        return None
    caps = [w for w in words if w.isupper() and len(w) >= 2 and not text.isupper()]
    return (caps or sorted(words, key=len, reverse=True))[0]


def title_png(text: str, path: Path, font_fn, width: int = W, highlight: str = "") -> int:
    """The big middle title: white lines around one huge, red, glowing key word. Returns image height."""
    import render
    text = render.EMOJI.sub("", text).strip().upper()
    hl = re.sub(r"[^A-Z0-9']", "", (highlight or "").upper())
    key = hl if hl and hl in re.sub(r"[^A-Z0-9' ]", "", text).split() else keyword(text)
    if key and key.upper() in text.split():
        i = text.split().index(key.upper())
        words = text.split()
        parts = [(" ".join(words[:i]), False), (key.upper(), True), (" ".join(words[i + 1:]), False)]
    else:
        parts = [(text, False)]
    parts = [(t, k) for t, k in parts if t]
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    rendered = []
    for t, is_key in parts:
        size = 150 if is_key else 78
        f = font_fn(size, t)
        while probe.textlength(t, font=f) > width - 90 and size > 40:
            size -= 6
            f = font_fn(size, t)
        rendered.append((t, is_key, f, size))
    gap = 6
    heights = [int(s * 1.08) for _, _, _, s in rendered]
    h = sum(heights) + gap * (len(rendered) - 1) + 40
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    glow = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d, g = ImageDraw.Draw(img), ImageDraw.Draw(glow)
    y = 20
    for (t, is_key, f, size), lh in zip(rendered, heights):
        cy = y + lh / 2
        if is_key:
            g.text((width / 2, cy), t, font=f, anchor="mm", fill=ACCENT + (255,))
            d.text((width / 2, cy), t, font=f, anchor="mm", fill=ACCENT, stroke_width=4, stroke_fill="white")
        else:
            d.text((width / 2 + 4, cy + 5), t, font=f, anchor="mm", fill=(0, 0, 0, 170))     # drop shadow
            d.text((width / 2, cy), t, font=f, anchor="mm", fill="white", stroke_width=5, stroke_fill="black")
        y += lh + gap
    out = Image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)), img)
    out.save(path)
    return h


def _content_crop(cam, src_w: int, src_h: int, aspect: float) -> tuple[int, int, int, int]:
    """A full-height crop of the given aspect (w/h) from the side of the picture away from the webcam,
    so the content shows without the webcam repeated in it."""
    cw = min(src_w, int(src_h * aspect)) // 2 * 2
    if cam is None or cam.full_frame:
        cx = cam.cx if cam is not None and cam.cx >= 0 else src_w // 2
    else:
        cam_mid = cam.x + cam.w / 2
        cx = (src_w - cw / 2) if cam_mid < src_w / 2 else cw / 2       # hug the opposite side
    x = int(min(max(0, cx - cw / 2), src_w - cw)) // 2 * 2
    return x, 0, cw, src_h // 2 * 2


def _cam_crop(cam, aspect: float, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """Crop the webcam box to an aspect, centred on the face."""
    bw, bh = cam.w, cam.h
    if bw / bh > aspect:
        cw, ch = int(bh * aspect), bh
    else:
        cw, ch = bw, int(bw / aspect)
    fx = cam.cx if cam.cx >= 0 else cam.x + bw // 2
    fy = cam.cy if cam.cy >= 0 else cam.y + bh // 2
    x = min(max(cam.x, fx - cw // 2), cam.x + bw - cw)
    y = min(max(cam.y, fy - int(ch * 0.45)), cam.y + bh - ch)
    x, y = max(0, min(x, src_w - cw)), max(0, min(y, src_h - ch))
    return x // 2 * 2, y // 2 * 2, cw // 2 * 2, ch // 2 * 2


def build(style: str, cam, src_w: int, src_h: int, title_h: int, follow: list[tuple[float, int]] | None = None) -> Layout:
    """follow: face positions over time (fullscreen with a full-frame camera): the crop glides with the face."""
    sharpen = "unsharp=5:5:0.7"
    if style == "split":
        top_h = 960
        gx, gy, gw, gh = _content_crop(cam, src_w, src_h, W / top_h)
        cx, cy, cw, ch = _cam_crop(cam, W / (H - top_h), src_w, src_h)
        f = (f"[0:v]split=2[g][c];"
             f"[g]crop={gw}:{gh}:{gx}:{gy},scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h}[top];"
             f"[c]crop={cw}:{ch}:{cx}:{cy},scale={W}:{H - top_h}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H - top_h},{sharpen}[bot];"
             f"[top][bot]vstack,setsar=1[v0];"
             f"[v0][1:v]overlay=0:{top_h - title_h // 2}:shortest=1[v1]")
        return Layout(f, True, 1580, "split")
    if style == "reaction":
        cam_h = 1180
        cx, cy, cw, ch = _cam_crop(cam, W / cam_h, src_w, src_h)
        game_h = int(W * src_h / src_w) // 2 * 2
        f = (f"[0:v]split=2[g][c];"
             f"[c]crop={cw}:{ch}:{cx}:{cy},scale={W}:{cam_h}:force_original_aspect_ratio=increase,"
             f"crop={W}:{cam_h},{sharpen}[cam];"
             f"[g]scale={W}:{game_h}[game];"
             f"[cam][game]vstack,pad={W}:{H}:0:0:black,setsar=1[v0];"
             f"[v0][1:v]overlay=0:60:shortest=1[v1]")
        return Layout(f, True, cam_h - 230, "reaction")
    if style == "fullscreen":
        gx, gy, gw, gh = _content_crop(cam, src_w, src_h, W / H)
        x = str(gx)
        if follow and (cam is None or cam.full_frame) and gw < src_w:
            import facecam
            keys = facecam.camera_path(follow, follow[-1][0] + 1, src_w, gw)
            if len(keys) > 2:
                x = "'" + facecam.x_expr(keys) + "'"
            elif keys:
                x = str(keys[0][1])
        fill = f"crop={gw}:{gh}:{x}:{gy},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        f = f"[0:v]{fill}"
        if cam is not None and not cam.full_frame:       # webcam bubble in the top corner
            cx, cy, cw, ch = _cam_crop(cam, 4 / 3, src_w, src_h)
            side = "40" if cam.x + cam.w / 2 < src_w / 2 else f"{W - 40 - 440}"
            f = (f"[0:v]split=2[g][c];[g]{fill}[bg];"
                 f"[c]crop={cw}:{ch}:{cx}:{cy},scale=440:330,{sharpen},"
                 f"pad=452:342:6:6:white[cam];[bg][cam]overlay={side}:{300 + title_h}[v0];")
        else:
            f += "[v0];"
        f += "[v0][1:v]overlay=0:110:shortest=1,setsar=1[v1]"
        return Layout(f, True, 1380, "fullscreen")
    raise ValueError(style)
