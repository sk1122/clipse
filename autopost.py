"""Auto-post every new clip a job makes, as soon as it's rendered (`clipse run --post-to ...`).

Uploads run one at a time on a background thread, so a slow upload never holds up live clipping. Clips the
judge skipped are never posted. Each platform's daily cap is respected; when it's reached, the rest are
left for `clipse post --auto` later.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import progress

_q: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_cfg: dict = {}


def start(platforms: list[str], info: dict, url: str, privacy: str = "private", max_per_day: int = 5,
          prof: dict | None = None) -> list[str]:
    """Begin auto-posting. Returns the platforms that are actually connected and will be used."""
    global _worker
    import instagram
    import youtube
    usable = []
    for p in platforms:
        mod = youtube if p == "youtube" else instagram
        if not mod.connected():
            msg = (f"auto-post to {p} is off: not connected "
                   f"(run ./clipse connect {p}{' and add INSTAGRAM_ACCESS_TOKEN to .env' if p == 'instagram' else ''})")
            print(f"  {msg}")
            progress.emit("post_error", platform=p, message=msg)
        else:
            usable.append(p)
    if not usable:
        return []
    _cfg.update(platforms=usable, info=info, url=url, privacy=privacy, max_per_day=max_per_day, prof=prof or {})
    progress.on_clip(_enqueue)
    _worker = threading.Thread(target=_run, daemon=True)
    _worker.start()
    print(f"  Auto-posting new clips to: {', '.join(usable)}"
          + (" (YouTube: private)" if "youtube" in usable and privacy == "private" else "")
          + (" (Instagram: public)" if "instagram" in usable else ""))
    return usable


def _enqueue(clip: dict) -> None:
    if clip.get("file") and not clip.get("skipped") and not clip.get("claude_skipped"):
        _q.put(clip)


def _source(clip: dict) -> str | None:
    info, url = _cfg["info"], _cfg["url"]
    if clip.get("part") != "live" and clip.get("start") is not None and info.get("id"):
        return f"https://youtu.be/{info['id']}?t={int(clip['start'])}"
    return url


def _run() -> None:
    import instagram
    import youtube
    while True:
        clip = _q.get()
        if clip is None:
            _q.task_done()
            return
        f = Path(clip["file"])
        for p in list(_cfg["platforms"]):
            mod = youtube if p == "youtube" else instagram
            try:
                if mod.posted(f):
                    continue
                today = youtube.uploads_today() if p == "youtube" else instagram.posts_today()
                if today >= _cfg["max_per_day"]:
                    msg = f"daily limit ({_cfg['max_per_day']}) reached — {f.name} left for `clipse post --auto`"
                    print(f"    [{p}] {msg}")
                    progress.emit("post_error", platform=p, file=str(f), message=msg)
                    continue
                print(f"    [{p}] posting {f.name}…")
                if p == "youtube":
                    rec = youtube.upload(f, clip, _source(clip), _cfg["privacy"], prof=_cfg["prof"])
                else:
                    rec = instagram.upload(f, clip, _source(clip), prof=_cfg["prof"])
                progress.emit("posted", platform=p, file=str(f), url=rec.get("url"),
                              privacy=rec.get("privacy", "public"))
            except youtube.LimitReached as e:
                print(f"    [{p}] {e} Stopping YouTube auto-posts for this run.")
                progress.emit("post_error", platform=p, file=str(f), message=str(e))
                _cfg["platforms"] = [x for x in _cfg["platforms"] if x != "youtube"]
            except Exception as e:     # one failed upload must never stop clipping
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"    [{p}] ✗ upload failed: {msg}")
                progress.emit("post_error", platform=p, file=str(f), message=msg)
        _q.task_done()


def finish() -> None:
    """Wait for queued uploads to finish (called when the job ends)."""
    if _worker and _worker.is_alive():
        if _q.unfinished_tasks:
            print(f"Finishing {_q.unfinished_tasks} upload(s)…")
        _q.put(None)
        _worker.join()
