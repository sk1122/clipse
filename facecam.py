"""Find the streamer's webcam in a stream video.

1. Detect faces in a handful of frames and keep the face that stays in the same place (the streamer;
   faces in the game move around).
2. Webcam overlays are fixed rectangles while the gameplay behind them changes, so average the edge maps of
   those frames: the overlay's border survives as strong, steady lines. Walk outward from the face to the
   first steady line on each side — that's the webcam box.
3. If no border is found (a full-screen camera, or a borderless cam), fall back to a box sized from the face.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_W = 960          # frames are analysed at this width


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int
    full_frame: bool = False     # the camera IS the video (IRL / just chatting), not an overlay
    cx: int = -1                 # face centre in source pixels (where a crop should be centred)
    cy: int = -1

    def scaled(self, k: float) -> "Box":
        return Box(int(self.x * k), int(self.y * k), int(self.w * k), int(self.h * k), self.full_frame,
                   int(self.cx * k), int(self.cy * k))


def _frames(src: Path, start: float, length: float, n: int = 10) -> tuple[list[np.ndarray], float]:
    import cv2
    cap = cv2.VideoCapture(str(src))
    out, scale = [], 1.0
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + length * (i + 0.5) / n) * 1000)
        ok, fr = cap.read()
        if not ok:
            continue
        scale = fr.shape[1] / SAMPLE_W
        out.append(cv2.resize(fr, (SAMPLE_W, int(fr.shape[0] / scale))))
    cap.release()
    return out, scale


def _steady_face(frames: list[np.ndarray]) -> tuple[int, int, int, int, float] | None:
    """The streamer's face: seen in about the same place in most frames, but alive (a real face moves a
    little; a face in posters, thumbnails or menu art doesn't). Ties go to the face nearest a frame edge,
    since webcams sit at the edges and corners."""
    import cv2
    det = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    grays, hits = [], []
    for i, fr in enumerate(frames):
        g = cv2.equalizeHist(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY))
        grays.append(g)
        for (x, y, w, h) in det.detectMultiScale(g, scaleFactor=1.08, minNeighbors=5, minSize=(20, 20)):
            hits.append((x + w / 2, y + h / 2, w, h, i))
    if not hits:
        return None
    H, W = grays[0].shape
    clusters = []
    for cx, cy, w, h, _ in hits:
        near = [hh for hh in hits if abs(hh[0] - cx) < w * 0.5 and abs(hh[1] - cy) < h * 0.5]
        n = len({hh[4] for hh in near})
        if n < max(3, len(frames) // 3):
            continue
        mx, my, mw, mh = np.median(np.array([hh[:4] for hh in near]), axis=0)
        x0, y0, x1, y1 = int(mx - mw / 2), int(my - mh / 2), int(mx + mw / 2), int(my + mh / 2)
        crops = [g[max(0, y0):y1, max(0, x0):x1].astype(float) for g in grays]
        motion = float(np.mean([np.abs(a - b).mean() for a, b in zip(crops, crops[1:])]))
        if motion < 3.0:                        # frozen: artwork, not a person
            continue
        edge = min(x0, y0, W - x1, H - y1) / max(W, H)
        clusters.append((n, x1 - x0, -edge, (x0, y0, x1 - x0, y1 - y0), n / len(frames)))
    if not clusters:
        return None
    clusters.sort(reverse=True)
    best_n = clusters[0][0]
    # among faces seen as often as the best one: the most prominent (biggest), then the one nearest an edge
    cands = [c for c in clusters if c[0] >= best_n]
    big = max(c[1] for c in cands)
    cands = [c for c in cands if c[1] >= big * 0.67]
    best = max(cands, key=lambda c: c[2])
    return (*best[3], best[4])


def _border(frames: list[np.ndarray], face: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """Find the fixed rectangle around the face from edges that don't move between frames."""
    import cv2
    H, W = frames[0].shape[:2]
    edges = np.mean([cv2.Canny(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), 60, 160) > 0 for f in frames], axis=0)
    steady = edges > 0.45                      # an edge present in most frames
    fx, fy, fw, fh = face
    # a border line must cover a good share of the span it bounds
    y_lo, y_hi = max(0, fy - fh), min(H, fy + 2 * fh)
    x_lo, x_hi = max(0, fx - fw), min(W, fx + 2 * fw)

    def col_score(x):
        return steady[y_lo:y_hi, max(0, x - 1):x + 2].any(axis=1).mean()

    def row_score(y):
        return steady[max(0, y - 1):y + 2, x_lo:x_hi].any(axis=0).mean()

    def walk(rng, score):
        for v in rng:
            if score(v) > 0.45:
                return v
        return None

    left = walk(range(fx - 2, -1, -1), col_score)
    right = walk(range(fx + fw + 2, W), col_score)
    top = walk(range(fy - 2, -1, -1), row_score)
    bottom = walk(range(fy + fh + 2, H), row_score)
    # cams often sit flush against an edge of the picture, where there's no line to find
    left = 0 if left is None and fx < W * 0.25 else left
    right = W - 1 if right is None and fx + fw > W * 0.75 else right
    top = 0 if top is None and fy < H * 0.25 else top
    bottom = H - 1 if bottom is None and fy + fh > H * 0.75 else bottom
    if None in (left, right, top, bottom):
        return None
    w, h = right - left, bottom - top
    # sanity: a webcam box is wider than the face, not huge, and roughly 4:3..16:9
    if not (fw * 1.6 < w < W * 0.6 and fh * 1.5 < h < H * 0.7 and 0.9 < w / h < 2.4):
        return None
    return left, top, w, h


def _moving_camera(frames: list[np.ndarray]) -> Box | None:
    """IRL / vlog footage: no webcam overlay, but people fill the picture in most frames. Returns a
    full-frame box centred on where the faces mostly are, or None for gameplay with no camera at all."""
    import cv2
    det = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    H, W = frames[0].shape[:2]
    centres = []
    for fr in frames:
        g = cv2.equalizeHist(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY))
        faces = [f for f in det.detectMultiScale(g, 1.1, 6, minSize=(int(W * 0.06), int(W * 0.06)))]
        if faces:
            x, y, w, h = max(faces, key=lambda f: f[2])
            centres.append((x + w / 2, y + h / 2))
    if len(centres) < len(frames) * 0.5:
        return None
    cx, cy = np.median(np.array(centres), axis=0)
    return Box(0, 0, W, H, True, int(cx), int(cy))


def _iou(a: Box, b: Box) -> float:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter / float(a.w * a.h + b.w * b.h - inter or 1)


def find(src: Path, start: float, length: float, memory: Path | None = None) -> Box | None:
    """The streamer's webcam box in `src` for this clip. With `memory` (a JSON file per stream), the box
    is checked against what earlier clips of the same stream found: a webcam doesn't move during a stream,
    so if most clips agree on a spot, a clip that disagrees (a poster face, a cutscene) uses the majority."""
    import json
    box = _find(src, start, length)
    if memory is None:
        return box
    seen = json.loads(memory.read_text()) if memory.exists() else []
    if box:
        seen.append([box.x, box.y, box.w, box.h, box.full_frame, box.cx, box.cy])
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text(json.dumps(seen[-40:]))
    boxes = [Box(*b) for b in seen if not b[4]]
    if box is not None and box.full_frame:
        return box                             # the stream switched to a full-screen camera scene
    if len(boxes) < 3:
        return box
    # the webcam doesn't move during a stream: use the median of the boxes most clips agree on
    best = max(boxes, key=lambda b: sum(_iou(b, o) > 0.5 for o in boxes))
    agree = [o for o in boxes if _iou(best, o) > 0.5]
    if len(agree) < max(3, len(boxes) // 2):
        return box
    m = np.median(np.array([[o.x, o.y, o.w, o.h, o.cx, o.cy] for o in agree]), axis=0).astype(int)
    return Box(int(m[0]), int(m[1]), int(m[2]), int(m[3]), False, int(m[4]), int(m[5]))


def _find(src: Path, start: float, length: float) -> Box | None:
    """Run detection in a separate process: OpenCV and the speech model's video library each bundle their
    own FFmpeg, and loading both in one process is unsafe on macOS."""
    import json
    import subprocess
    import sys
    r = subprocess.run([sys.executable, __file__, str(src), str(start), str(length)],
                       capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return Box(**d) if d else None


def _detect(src: Path, start: float, length: float) -> Box | None:
    try:
        frames, scale = _frames(src, start, length)
    except Exception:
        return None
    if len(frames) < 4:
        return None
    H, W = frames[0].shape[:2]
    face = _steady_face(frames)
    if not face:
        return _moving_camera(frames)
    fx, fy, fw, fh, seen = face
    face = (fx, fy, fw, fh)
    if fw > W * 0.12 and seen >= 0.7:          # a big face in nearly every frame: the camera is the picture
        return Box(0, 0, W, H, True, fx + fw // 2, fy + fh // 2).scaled(scale)
    box = _border(frames, face)
    if box is None and fw > W * 0.06:          # a large face with no frame around it: a real camera shot
        return Box(0, 0, W, H, True, fx + fw // 2, fy + fh // 2).scaled(scale)
    if box is None:                            # no clear border: frame the face generously, 16:10
        w = min(W, int(fw * 4.2))
        h = min(H, int(w / 1.6))
        x = int(min(max(0, fx + fw / 2 - w / 2), W - w))
        y = int(min(max(0, fy + fh * 0.6 - h / 2), H - h))
        box = (x, y, w, h)
    return Box(*box, False, fx + fw // 2, fy + fh // 2).scaled(scale)


def _track(src: Path, start: float, length: float, step: float = 0.5) -> list[tuple[float, int]]:
    """Where the main face is (x centre, source pixels) every `step` seconds. The biggest face wins, but the
    one we were already following keeps it unless another is clearly bigger, so the crop doesn't flip."""
    import cv2
    det = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    cap = cv2.VideoCapture(str(src))
    out, prev = [], None
    t = 0.0
    while t < length:
        cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
        ok, fr = cap.read()
        if not ok:
            break
        scale = fr.shape[1] / SAMPLE_W
        g = cv2.equalizeHist(cv2.cvtColor(cv2.resize(fr, (SAMPLE_W, int(fr.shape[0] / scale))), cv2.COLOR_BGR2GRAY))
        faces = det.detectMultiScale(g, scaleFactor=1.1, minNeighbors=6, minSize=(40, 40))
        if len(faces):
            faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
            pick = faces[0]
            if prev is not None:
                near = min(faces, key=lambda f: abs(f[0] + f[2] / 2 - prev))
                if near[2] * near[3] * 1.8 >= pick[2] * pick[3]:
                    pick = near
            prev = pick[0] + pick[2] / 2
            out.append((round(t, 2), int(prev * scale)))
        t += step
    cap.release()
    return out


def track(src: Path, start: float, length: float) -> list[tuple[float, int]]:
    """Face positions over the clip, detected in a separate process (see _find)."""
    import json
    import subprocess
    import sys
    r = subprocess.run([sys.executable, __file__, "track", str(src), str(start), str(length)],
                       capture_output=True, text=True, timeout=300)
    try:
        return [tuple(x) for x in json.loads(r.stdout.strip().splitlines()[-1])]
    except (ValueError, IndexError):
        return []


def camera_path(points: list[tuple[float, int]], length: float, src_w: int, crop_w: int,
                dead: float = 0.08, max_keys: int = 40) -> list[tuple[float, int]]:
    """Turn raw face positions into calm camera moves: (time, crop x) keyframes. Jitter is smoothed away,
    the camera holds still until the face moves more than `dead` of the frame width, then glides over ~0.6 s."""
    if len(points) < 3:
        return []
    xs = np.array([p[1] for p in points], float)
    ts = np.array([p[0] for p in points], float)
    k = 2                                                   # median filter: one-off misdetections vanish
    xs = np.array([np.median(xs[max(0, i - k):i + k + 1]) for i in range(len(xs))])
    lo, hi = 0, src_w - crop_w
    to_x = lambda c: int(min(max(lo, c - crop_w / 2), hi)) // 2 * 2
    keys = [(0.0, to_x(xs[0]))]
    cur = xs[0]
    for t, x in zip(ts[1:], xs[1:]):
        if abs(x - cur) > dead * src_w:
            keys.append((max(keys[-1][0] + 0.05, t - 0.6), keys[-1][1]))   # hold, then glide
            keys.append((t, to_x(x)))
            cur = x
    keys.append((length, keys[-1][1]))
    if len(keys) > max_keys:                                 # too busy: keep the biggest moves
        keys = keys[:1] + keys[1:-1][: max_keys - 2] + keys[-1:]
    return keys


def x_expr(keys: list[tuple[float, int]]) -> str:
    """An ffmpeg expression in t for the crop x, gliding between keyframes."""
    expr = str(keys[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        if x0 == x1:
            seg = str(x0)
        else:
            seg = f"{x0}+({x1 - x0})*(t-{t0:.2f})/{max(0.01, t1 - t0):.2f}"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    return expr


if __name__ == "__main__":
    import json
    import sys
    from dataclasses import asdict
    if sys.argv[1] == "track":
        print(json.dumps(_track(Path(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]))))
    else:
        b = _detect(Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]))
        print(json.dumps(asdict(b) if b else None))
