"""Auto-post every new clip a job makes, as soon as it's rendered (`clipse run --post-to ...`).

Clips go into the shared posting queue (postqueue.py); one background poster uploads them spread across
the day, best first, within each platform's daily cap. Clips the judge skipped are never posted.
"""
from __future__ import annotations

from pathlib import Path

import postqueue
import progress

_cfg: dict = {}


def start(platforms: list[str], info: dict, url: str, privacy: str = "private", max_per_day: int = 40,
          prof: dict | None = None, gap_min: float = 0, hours: str = postqueue.DEFAULT_HOURS) -> list[str]:
    """Begin auto-posting. Returns the platforms that are actually connected and will be used."""
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
    _cfg.update(platforms=usable, info=info, url=url, privacy=privacy, max_per_day=max_per_day, prof=prof or {},
                gap=gap_min, hours=hours)
    progress.on_clip(_enqueue)
    gap = postqueue.gap_seconds(max_per_day, gap_min, hours) / 60
    print(f"  Auto-posting new clips to: {', '.join(usable)}"
          + (" (YouTube: private)" if "youtube" in usable and privacy == "private" else "")
          + (" (Instagram: public)" if "instagram" in usable else "")
          + f"\n  Posts are spread out: best clip first, one every ~{gap:.0f} min, {hours} h, max {max_per_day}/day")
    return usable


def _enqueue(clip: dict) -> None:
    if not clip.get("file") or clip.get("skipped") or clip.get("claude_skipped"):
        return
    for p in _cfg["platforms"]:
        if postqueue.add(clip["file"], p, clip, _source(clip), prof=_cfg["prof"], privacy=_cfg["privacy"],
                         max_per_day=_cfg["max_per_day"], gap_min=_cfg["gap"], hours=_cfg["hours"]):
            print(f"    [{p}] queued {Path(clip['file']).name}")
            progress.emit("post_queued", platform=p, file=clip["file"])
    postqueue.ensure_poster()


def _source(clip: dict) -> str | None:
    info, url = _cfg["info"], _cfg["url"]
    if clip.get("part") != "live" and clip.get("start") is not None and info.get("id"):
        return f"https://youtu.be/{info['id']}?t={int(clip['start'])}"
    return url


def finish() -> None:
    """Called when the job ends. Queued clips keep posting from the background poster."""
    if _cfg.get("platforms"):
        for line in postqueue.status():
            print(f"  {line}")
