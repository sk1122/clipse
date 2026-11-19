"""Machine-readable progress for every job, so the TUI (or anything else) can follow it.

Each job folder (out/<video id>/) gets:
  job.json      — what's running: url, title, mode (live/vod), pid, settings, start time
  events.jsonl  — one JSON object per line: stage changes, live stats, spikes, clips, finish
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_file = None
_job_path: Path | None = None


def setup(job_dir: Path, url: str, info: dict, mode: str, settings: dict) -> None:
    global _file, _job_path
    job_dir.mkdir(parents=True, exist_ok=True)
    _job_path = job_dir / "job.json"
    _job_path.write_text(json.dumps({
        "url": url, "id": info.get("id"), "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"), "mode": mode, "pid": os.getpid(),
        "started": time.time(), "duration": info.get("duration"), "settings": settings,
    }, ensure_ascii=False, indent=2))
    _file = open(job_dir / "events.jsonl", "a", buffering=1)
    emit("start", mode=mode)


_listeners: list = []


def on_clip(callback) -> None:
    """Call `callback(clip_event)` for every clip announced from now on (used by auto-posting)."""
    _listeners.append(callback)


def emit(kind: str, **data) -> None:
    if _file is not None:
        with _lock:
            _file.write(json.dumps({"t": round(time.time(), 2), "kind": kind, **data}, ensure_ascii=False) + "\n")
    if kind == "clip":
        for cb in _listeners:
            cb(data)


def stage(n: int, of: int, name: str, detail: str = "", part: str = "main", frac: float | None = None) -> None:
    """A step in a pipeline: e.g. stage(2, 5, 'Collecting signals'). frac = progress within the step (0-1)."""
    emit("stage", n=n, of=of, name=name, detail=detail, part=part, frac=frac)
