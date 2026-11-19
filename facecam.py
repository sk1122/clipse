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


if __name__ == "__main__":
    import json
    import sys
    from dataclasses import asdict
    b = _detect(Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]))
    print(json.dumps(asdict(b) if b else None))
