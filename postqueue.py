"""One shared posting queue, so clips go out spread across the day instead of in bursts.

Jobs don't upload anything themselves: they add each finished clip to `out/_post/queue.json`. A single
background poster (`clipse poster`, started automatically when something is queued) takes the best clip
waiting for each platform whenever that platform's next slot opens:

  * slots are spread evenly over the posting hours (default 09:00-01:00 local), e.g. a cap of 40 over
    16 hours = one post about every 24 minutes; `--post-gap` sets the gap yourself
  * the best clip waiting goes first (judge's virality, then spike score), with newer clips nudged ahead
  * the daily cap is checked inside the poster, which is the only process posting, so it can't overshoot

Only one poster runs at a time (a lock file), and it exits after an hour with nothing to post.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIR = HERE / "out" / "_post"
QUEUE = DIR / "queue.json"
DEFAULT_HOURS = "9-1"          # local time: 09:00 to 01:00
IDLE_EXIT = 3600               # the poster quits after this long with an empty queue


@contextmanager
def _locked(name: str = "queue.lock", block: bool = True):
    DIR.mkdir(parents=True, exist_ok=True)
    fh = open(DIR / name, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except BlockingIOError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _load() -> list[dict]:
    try:
        return json.loads(QUEUE.read_text())
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False))
    tmp.replace(QUEUE)


def add(file: str | Path, platform: str, meta: dict, source: str | None, *, prof: dict | None = None,
        privacy: str = "private", max_per_day: int = 40, gap_min: float = 0, hours: str = DEFAULT_HOURS) -> bool:
    """Queue a clip for a platform. Returns False if it's already queued."""
    f = str(Path(file).resolve())
    keep = {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool, type(None)))}
    with _locked():
        items = _load()
        if any(i["file"] == f and i["platform"] == platform for i in items):
            return False
        items.append({"file": f, "platform": platform, "meta": keep, "source": source, "prof": prof or {},
                      "privacy": privacy, "max": max_per_day, "gap": gap_min, "hours": hours,
                      "added": time.time()})
        _save(items)
    return True


def queued(file: str | Path, platform: str | None = None) -> bool:
    f = str(Path(file).resolve())
    return any(i["file"] == f and (platform is None or i["platform"] == platform) for i in _load())


def pending(platform: str | None = None) -> list[dict]:
    return [i for i in _load() if platform is None or i["platform"] == platform]


def remove(file: str, platform: str) -> None:
    with _locked():
        _save([i for i in _load() if not (i["file"] == file and i["platform"] == platform)])


def _priority(i: dict) -> float:
    m = i["meta"]
    vir = float(m.get("virality") or 5)
    spike = float(m.get("score") or m.get("peak_z") or 0)
    age_h = (time.time() - i.get("added", time.time())) / 3600
    return vir + min(spike, 10) * 0.1 - min(age_h, 24) * 0.05   # fresh moments matter more


# ---------------------------------------------------------------- timing

def _hours(spec: str) -> tuple[int, int]:
    try:
        a, b = (int(x) % 24 for x in spec.split("-"))
        return a, b
    except ValueError:
        return 9, 1


def in_hours(spec: str, now: datetime | None = None) -> bool:
    a, b = _hours(spec)
    h = (now or datetime.now()).hour
    return a == b or (a <= h < b if a < b else h >= a or h < b)


def window_hours(spec: str) -> int:
    a, b = _hours(spec)
    return 24 if a == b else (b - a) % 24


def gap_seconds(max_per_day: int, gap_min: float, spec: str) -> float:
    """Minutes between posts: the user's gap, or the posting hours spread evenly over the daily cap."""
    if gap_min and gap_min > 0:
        return gap_min * 60
    return max(10 * 60, window_hours(spec) * 3600 / max(1, max_per_day))


def _mod(platform: str):
    import instagram
    import youtube
    return youtube if platform == "youtube" else instagram


def last_post(platform: str) -> float:
    mod = _mod(platform)
    recs = mod._posts().values()
    ts = [datetime.fromisoformat(r["at"]).timestamp() for r in recs if r.get("at")]
    return max(ts, default=0.0)


def today_count(platform: str) -> int:
    mod = _mod(platform)
    return mod.uploads_today() if platform == "youtube" else mod.posts_today()


def next_reset() -> datetime:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def next_slot(platform: str) -> tuple[float, str]:
    """When this platform may post next, and why it's waiting (for status displays)."""
    items = pending(platform)
    if not items:
        return 0.0, "nothing queued"
    best = max(items, key=_priority)
    if today_count(platform) >= best["max"]:
        return next_reset().timestamp(), f"daily limit ({best['max']}) reached"
    t = last_post(platform) + gap_seconds(best["max"], best["gap"], best["hours"])
    if not in_hours(best["hours"]):
        a, _ = _hours(best["hours"])
        now = datetime.now()
        start = now.replace(hour=a, minute=0, second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)
        return max(t, start.timestamp()), f"outside posting hours ({best['hours']})"
    return t, "spacing posts out"


def status() -> list[str]:
    lines = []
    for p in ("instagram", "youtube"):
        n = len(pending(p))
        if not n:
            continue
        t, why = next_slot(p)
        when = "now" if t <= time.time() else datetime.fromtimestamp(t).strftime("%H:%M")
        lines.append(f"{p}: {n} queued, next post {when} ({why})")
    return lines


# ---------------------------------------------------------------- the poster

def poster_running() -> bool:
    with _locked("poster.lock", block=False) as got:
        return not got


def ensure_poster() -> None:
    """Start the background poster if it isn't running."""
    if poster_running():
        return
    log = HERE / "out" / "_logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-poster.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, "-u", str(HERE / "clip.py"), "poster"], stdout=open(log, "w"),
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, cwd=HERE)


def _post_one(i: dict, log) -> None:
    import youtube
    f = Path(i["file"])
    mod = _mod(i["platform"])
    if not f.exists() or mod.posted(f):
        remove(i["file"], i["platform"])
        return
    log(f"[{time.strftime('%H:%M')}] [{i['platform']}] posting {f.name} (score {_priority(i):.1f})…")
    try:
        if i["platform"] == "youtube":
            rec = youtube.upload(f, i["meta"], i["source"], i["privacy"], prof=i["prof"], log=log)
        else:
            rec = mod.upload(f, i["meta"], i["source"], prof=i["prof"], log=log)
        remove(i["file"], i["platform"])
        _emit(f, "posted", platform=i["platform"], file=str(f), url=rec.get("url"),
              privacy=rec.get("privacy", "public"))
        log(f"    ✓ {rec.get('url') or 'posted'}")
    except youtube.LimitReached as e:
        log(f"    {e}")
        raise
    except Exception as e:      # leave it queued; it's retried at the next slot (at most 3 times)
        i["fails"] = i.get("fails", 0) + 1
        log(f"    ✗ {type(e).__name__}: {str(e)[:200]}")
        with _locked():
            items = _load()
            for x in items:
                if x["file"] == i["file"] and x["platform"] == i["platform"]:
                    x["fails"] = i["fails"]
            _save([x for x in items if x.get("fails", 0) < 3])
        _emit(f, "post_error", platform=i["platform"], file=str(f), message=f"{type(e).__name__}: {str(e)[:200]}")


def _emit(f: Path, kind: str, **kw) -> None:
    """Write the event into the clip's own job, so the TUI shows ▶ig / errors there."""
    for parent in f.parents:
        ev = parent / "events.jsonl"
        if ev.exists():
            with open(ev, "a") as fh:
                fh.write(json.dumps({"t": time.time(), "kind": kind, **kw}, ensure_ascii=False) + "\n")
            return


def run_poster(log=print, once: bool = False) -> int:
    """Post queued clips at their slots until the queue has been empty for an hour. Returns clips posted."""
    import youtube
    with _locked("poster.lock", block=False) as got:
        if not got:
            log("Another poster is already running.")
            return 0
        os.environ.setdefault("PYTHONUNBUFFERED", "1")
        posted, idle_since, blocked = 0, None, {}
        last_status = ""
        while True:
            now = time.time()
            items = _load()
            if not items:
                idle_since = idle_since or now
                if once or now - idle_since > IDLE_EXIT:
                    log("Queue empty — poster stopping.")
                    return posted
            else:
                idle_since = None
            for p in ("instagram", "youtube"):
                if blocked.get(p, 0) > now or not pending(p):
                    continue
                t, why = next_slot(p)
                if t > now:
                    continue
                best = max(pending(p), key=_priority)
                try:
                    _post_one(best, log)
                    posted += 1
                except youtube.LimitReached:
                    blocked[p] = next_reset().timestamp()
            s = " · ".join(status())
            if s and s != last_status:
                log(f"[{time.strftime('%H:%M')}] {s}")
                last_status = s
            if once and all(next_slot(p)[0] > time.time() for p in ("instagram", "youtube") if pending(p)):
                return posted
            time.sleep(30)
