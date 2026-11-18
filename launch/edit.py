"""Edit the kattappa launch video (run from launch/ after recording part1_start.mp4 and part2_results.mp4 with
launch.tape and results.tape): camera zooms over the terminal recordings, title cards, a showcase
of a real generated reel, and an outro.  Run with the clipper venv:  ../.venv/bin/python edit.py"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
W, H, FPS = 1920, 1080, 30
BG = (17, 17, 27)          # Catppuccin Mocha crust, matches the terminal theme
ACCENT = (166, 227, 161)   # green, like the Start button
TEXT = (205, 214, 244)
MUTED = (147, 153, 178)
FONT_BIG = "/System/Library/Fonts/Supplemental/Arial Black.ttf"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
WORK = HERE / "build"
SPEED = 2.0                 # screen recordings play at this speed
REEL_CAPTIONS = ["picked, trimmed,\ncaptioned,\nready to post", "with an optional\nhook intro"]
WORK.mkdir(exist_ok=True)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def ease(x: float) -> float:          # smooth in-out
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def camera_at(t: float, keys: list[tuple]) -> tuple[float, float, float]:
    """keys: (time, zoom, cx, cy). Between consecutive keys the camera eases from one to the next."""
    if t <= keys[0][0]:
        return keys[0][1:]
    for (t0, *a), (t1, *b) in zip(keys, keys[1:]):
        if t0 <= t <= t1:
            k = ease((t - t0) / max(t1 - t0, 1e-6))
            return tuple(a[i] + (b[i] - a[i]) * k for i in range(3))
    return keys[-1][1:]


def duration(path: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(path)], capture_output=True, text=True).stdout)


def encoder(out: Path, audio_from: Path | None = None) -> subprocess.Popen:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
           "-i", "-"]
    if audio_from:
        cmd += ["-i", str(audio_from), "-map", "0:v", "-map", "1:a", "-shortest"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(out)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def frames(src: Path, start: float = 0.0, length: float | None = None, speed: float = 1.0):
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start}", "-i", str(src)]
    if length:
        cmd += ["-t", f"{length}"]
    cmd += ["-vf", f"setpts=PTS/{speed},scale={W}:{H},fps={FPS}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = W * H * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield Image.frombytes("RGB", (W, H), buf)


def zoom_shot(src: Path, keys: list[tuple], out: Path, start: float = 0.0, length: float | None = None,
              fade_in: float = 0.0, fade_out: float = 0.0, speed: float = 1.0) -> Path:
    """Camera moves over a recording. `keys` are in the SOURCE's time; `speed` plays it faster."""
    total = (length or duration(src) - start) / speed
    enc = encoder(out)
    n = 0
    for img in frames(src, start, length, speed):
        t = n / FPS
        z, cx, cy = camera_at(t * speed, keys)
        cw, ch = W / z, H / z
        x0 = min(max(cx - cw / 2, 0), W - cw)
        y0 = min(max(cy - ch / 2, 0), H - ch)
        f = img.resize((W, H), Image.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch)) if z > 1.001 else img
        a = 1.0
        if fade_in and t < fade_in:
            a = t / fade_in
        if fade_out and t > total - fade_out:
            a = min(a, max(0.0, (total - t) / fade_out))
        if a < 1:
            f = Image.blend(Image.new("RGB", (W, H), BG), f, a)
        enc.stdin.write(f.tobytes())
        n += 1
    enc.stdin.close()
    enc.wait()
    return out


def card(lines: list[tuple[str, str, int, tuple]], out: Path, dur: float = 2.6, bg_img: Image.Image | None = None,
         sub_y: int | None = None) -> Path:
    """lines: (text, font path, size, colour), stacked and centred. Text fades/slides in."""
    base = bg_img.filter(ImageFilter.GaussianBlur(18)).point(lambda v: int(v * 0.35)) if bg_img else \
        Image.new("RGB", (W, H), BG)
    enc = encoder(out)
    total = int(dur * FPS)
    heights = [font(fp, sz).getbbox(tx)[3] + 24 for tx, fp, sz, _ in lines]
    y_start = (H - sum(heights)) // 2
    for i in range(total):
        t = i / FPS
        a_in = ease(t / 0.6)
        a_out = ease((dur - t) / 0.45)
        img = base.copy()
        d = ImageDraw.Draw(img)
        y = y_start + int((1 - a_in) * 30)
        for (tx, fp, sz, col), h in zip(lines, heights):
            c = tuple(int(BG[k] + (col[k] - BG[k]) * min(a_in, a_out)) for k in range(3))
            d.text((W / 2, y), tx, font=font(fp, sz), fill=c, anchor="mt")
            y += h
        enc.stdin.write(img.tobytes())
    enc.stdin.close()
    enc.wait()
    return out


def showcase(reel: Path, out: Path, caption: str, start: float = 0.0, length: float = 14.0,
             bg_img: Image.Image | None = None) -> Path:
    """A finished vertical reel playing in the middle of the frame, with a label beside it."""
    base = bg_img.filter(ImageFilter.GaussianBlur(22)).point(lambda v: int(v * 0.3)) if bg_img else \
        Image.new("RGB", (W, H), BG)
    rh = 1000
    rw = int(rh * 9 / 16)
    x0 = W // 2 - rw // 2 + 260
    y0 = (H - rh) // 2
    mask = Image.new("L", (rw, rh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, rw - 1, rh - 1], radius=36, fill=255)
    cut = WORK / "reel_cut.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start}", "-i", str(reel), "-t", f"{length}",
                    "-vf", f"scale={rw}:{rh},fps={FPS}", "-c:v", "libx264", "-crf", "14", "-c:a", "aac",
                    "-ar", "48000", "-ac", "2", str(cut)], check=True)
    enc = encoder(out, audio_from=cut)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(cut), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         stdout=subprocess.PIPE)
    lbl_font, small = font(FONT_BIG, 64), font(FONT, 34)
    i = 0
    while True:
        buf = p.stdout.read(rw * rh * 3)
        if len(buf) < rw * rh * 3:
            break
        t = i / FPS
        img = base.copy()
        a = ease(t / 0.5) * ease((length - t) / 0.4)
        fr = Image.frombytes("RGB", (rw, rh), buf)
        if a < 1:
            fr = Image.blend(Image.new("RGB", (rw, rh), BG), fr, a)
        img.paste(fr, (x0, y0), mask)
        d = ImageDraw.Draw(img)
        col = tuple(int(BG[k] + (TEXT[k] - BG[k]) * a) for k in range(3))
        acc = tuple(int(BG[k] + (ACCENT[k] - BG[k]) * a) for k in range(3))
        tx = x0 - 70
        d.text((tx, H / 2 - 70), "made by kattappa", font=small, fill=acc, anchor="rm")
        y = H / 2 - 10
        for line in caption.split("\n"):
            d.text((tx, y), line, font=lbl_font, fill=col, anchor="rm")
            y += 78
        enc.stdin.write(img.tobytes())
        i += 1
    enc.stdin.close()
    enc.wait()
    return out


def concat(parts: list[Path], out: Path) -> Path:
    lst = WORK / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], check=True)
    return out


def first_frame(src: Path, at: float = 0.0) -> Image.Image:
    return next(frames(src, at, 0.2))


if __name__ == "__main__":
    part1, part2 = HERE / "part1_start.mp4", HERE / "part2_results.mp4"
    reels = [Path(p) for p in sys.argv[1:]]          # finished clips to show off, best first
    full = (1.0, 960, 540)
    len1 = duration(part1)

    # --- part 1: paste link → choose captions → Start → it goes live
    k1 = [
        (0.0, *full), (0.9, *full),
        (1.6, 2.0, 500, 130),               # the link box, as the URL is typed
        (3.4, 2.0, 760, 130),
        (4.0, 2.1, 1000, 175),              # options row: captions → English
        (5.8, 2.1, 1000, 175),
        (6.4, 2.3, 1660, 150),              # the Start button
        (7.4, 2.0, 1500, 600),              # "Starting…" toast
        (8.4, 1.0, 960, 540),               # zoom out: the job is LIVE ●
        (10.2, 1.0, 960, 540),
        (11.4, 1.6, 1385, 330),             # live progress: chat rate, spike score, voice, catch-up bar
        (len1 - 1.6, 1.68, 1385, 335),
        (len1, 1.0, 960, 540),
    ]
    s1 = zoom_shot(part1, k1, WORK / "s1.mp4", fade_in=0.5, speed=SPEED)

    bg = first_frame(part1, 20)
    intro = card([("kattappa", FONT_BIG, 150, TEXT),
                  ("paste a livestream link → get the viral clips", FONT, 46, MUTED)], WORK / "intro.mp4", 3.0)
    mid = card([("…a few minutes later", FONT, 54, MUTED),
                ("the first clips drop", FONT_BIG, 86, ACCENT)], WORK / "mid.mp4", 2.4, bg_img=bg)
    parts = [intro, s1, mid]

    # --- part 2: the results in kattappa
    if part2.exists():
        k2 = [(0.0, *full), (1.2, *full), (2.6, 1.7, 1370, 470), (duration(part2) - 1.4, 1.7, 1370, 470),
              (duration(part2), *full)]
        parts.append(zoom_shot(part2, k2, WORK / "s2.mp4", speed=SPEED))
        bg = first_frame(part2, 2)

    # --- showcase the actual reels it made
    for i, reel in enumerate(reels[:2]):
        parts.append(showcase(reel, WORK / f"show{i}.mp4", caption=REEL_CAPTIONS[i] if i < len(REEL_CAPTIONS)
                              else "a ready-to-post\nvertical clip", bg_img=bg, length=min(14.0, duration(reel))))

    outro = card([("kattappa", FONT_BIG, 130, TEXT),
                  ("live chat + voice spikes  ·  AI picks & trims  ·  captions in English, Hinglish, हिंदी", FONT, 34,
                   MUTED),
                  ("clips while the stream is still on", FONT_BIG, 54, ACCENT)], WORK / "outro.mp4", 3.6)
    parts.append(outro)
    cut = concat(parts, WORK / "cut.mp4")

    # --- music: an original track generated in code (no licensing issues), dropping when the clips drop
    import music
    starts, t = [], 0.0
    for p in parts:
        starts.append(t)
        t += duration(p)
    total = t
    drop = starts[parts.index(mid) + 1]                      # the moment "the first clips drop" card ends
    reels_at = [(starts[i], starts[i] + duration(p)) for i, p in enumerate(parts) if p.name.startswith("show")]
    track = music.make_track(total, drop, WORK / "music.wav")
    duck = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in reels_at) or "0"
    final = HERE / "kattappa_launch.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(cut), "-i", str(track), "-filter_complex",
                    f"[1:a]volume='if({duck},0.2,1.0)':eval=frame,"
                    f"afade=t=out:st={total - 2.2:.2f}:d=2.2[m];"
                    f"[0:a][m]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,"
                    f"aresample=48000[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                    "-movflags", "+faststart", str(final)], check=True)
    print("wrote", final, f"{duration(final):.1f}s  (drop at {drop:.1f}s)")

