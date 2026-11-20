"""Find videos worth clipping by searching YouTube.

    clipse discover "gta 6 funny moments" "gta 6 rage" --mode live --expand 10
    clipse discover --queries-file queries.txt --mode week --top 5 --run --clips 25 --style split

Modes: live (streams on air now, ranked by live viewers), week (uploaded this week, ranked by views per
day), popular (all-time, ranked by views). --expand asks the judge model for more queries in the same
spirit. Videos clipse already has a job for are skipped. Every search is saved to out/_discover/.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILTERS = {                       # YouTube's own search filters
    "live": "EgJAAQ%3D%3D",       # live now
    "week": "EgIIAw%3D%3D",       # uploaded this week
    "popular": "CAM%3D",          # sorted by view count
}


def search(query: str, mode: str = "live", limit: int = 40) -> list[dict]:
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
           + "&sp=" + urllib.parse.quote(FILTERS[mode]))
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--flat-playlist", "-J",
                        "--playlist-end", str(limit), url], capture_output=True, text=True, timeout=120)
    try:
        entries = json.loads(r.stdout).get("entries") or []
    except ValueError:
        return []
    out = []
    for e in entries:
        if not e.get("id") or e.get("_type") not in (None, "url"):
            continue
        if "/shorts/" in (e.get("url") or ""):
            continue                                    # already a short: nothing to clip
        out.append({"id": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
                    "live": e.get("live_status") == "is_live", "viewers": e.get("concurrent_view_count"),
                    "views": e.get("view_count"), "duration": e.get("duration"),
                    "timestamp": e.get("timestamp") or e.get("release_timestamp"),
                    "description": (e.get("description") or "")[:300],
                    "url": f"https://www.youtube.com/watch?v={e['id']}", "query": query})
    return out


def expand(queries: list[str], n: int, model: str | None) -> list[str]:
    """More search queries in the same spirit, written by the judge model."""
    if not n or not model or not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    import anthropic
    from pydantic import BaseModel

    class Queries(BaseModel):
        queries: list[str]

    resp = anthropic.Anthropic().messages.parse(
        model=model, max_tokens=4000, output_config={"effort": "low"}, output_format=Queries,
        messages=[{"role": "user", "content":
            "These YouTube searches find livestreams and videos full of wild, funny or viral moments to clip "
            "into short-form content:\n" + "\n".join(f"- {q}" for q in queries) +
            f"\n\nWrite {n} NEW search queries in the same spirit that would surface different crazy, chaotic, "
            f"funny or hype videos (other creators, situations, reactions, slang people actually search). "
            "Stay on exactly the same topic as the list (the same game, event or subject: don't drift from "
            "GTA 6 to GTA 5). Short, natural YouTube search phrasing. No duplicates of the list above."}])
    seen = {q.lower() for q in queries}
    return [q.strip() for q in (resp.parsed_output.queries if resp.parsed_output else [])
            if q.strip() and q.strip().lower() not in seen][:n]


MIN_VIEWS = {"live": 0, "week": 1000, "popular": 10000}
GENERIC = set("""live stream streams streaming streamer funny funniest moments moment clips clip crazy insane wild best
epic rage raging reaction reactions chaos chaotic gameplay video videos new full part highlights highlight compilation
playing play plays first time hours hour today now day the a an of in on with and or for to my his her vs and
fails fail glitches glitch hilarious trailer match matches movement movements""".split())
RERUN = re.compile(r"24/7|24x7|\bbest of\b|non[- ]?stop|all episodes|season \d+\s*[-–]\s*\d+|\bradio\b|"
                   r"news live|lofi|music live", re.I)


def relevant(v: dict, query: str) -> bool:
    """The video is actually about the query (not a loosely related 24/7 stream YouTube padded in)."""
    if RERUN.search(v["title"]):
        return False
    keys = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t not in GENERIC and (len(t) > 2 or t.isdigit())]
    if not keys:
        return True
    hay = (v["title"] + " " + v["channel"]).lower()   # not the description: it's often stuffed with tags
    hay = re.sub(r"\bvi\b", "6", hay)               # GTA VI == GTA 6
    hay = re.sub(r"grand theft auto", "gta", hay)
    words = set(re.findall(r"[a-z0-9]+", hay))
    # numbers must match exactly (GTA 6 is not GTA 5); of the other distinctive words, at least half
    nums = [k for k in keys if k.isdigit()]
    rest = [k for k in keys if not k.isdigit()]
    if not all(k in words for k in nums):
        return False
    hit = sum(1 for k in rest if k in words or any(w.startswith(k[:5]) for w in words if len(k) > 5))
    return not rest or hit * 2 >= len(rest)


def score(v: dict, mode: str) -> float:
    if mode == "live":
        return float(v.get("viewers") or 0)
    views = float(v.get("views") or 0)
    if mode == "week" and v.get("timestamp"):
        days = max(0.25, (time.time() - v["timestamp"]) / 86400)
        return views / days                              # momentum: views per day
    return views


def already_clipped() -> set[str]:
    out = HERE / "out"
    return {d.name for d in out.iterdir() if (d / "job.json").exists()} if out.exists() else set()


def discover(queries: list[str], mode: str = "live", per_query: int = 40, expand_n: int = 0,
             model: str | None = None, min_minutes: float = 2, log=print) -> list[dict]:
    extra = expand(queries, expand_n, model)
    if extra:
        log(f"Generated {len(extra)} more queries:\n  " + "\n  ".join(extra))
    allq = queries + extra
    log(f"Searching YouTube ({mode}) for {len(allq)} queries…")
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda q: search(q, mode, per_query), allq))
    seen, skip, found = set(), already_clipped(), []
    for q, rs in zip(allq, results):
        for v in rs:
            if v["id"] in seen:
                for f in found:                          # remember every query that found it
                    if f["id"] == v["id"] and q not in f["queries"]:
                        f["queries"].append(q)
                continue
            seen.add(v["id"])
            if v["id"] in skip or not relevant(v, q):
                continue
            if mode != "live" and v.get("duration") and v["duration"] < min_minutes * 60:
                continue                                 # too short to have clip-worthy moments
            if mode != "live" and (v.get("views") or 0) < MIN_VIEWS[mode]:
                continue                                 # nobody watched it: rarely worth clipping
            v["queries"] = [q]
            v["score"] = score(v, mode)
            found.append(v)
    # found by several queries = more on-topic
    found.sort(key=lambda v: (v["score"] * (1 + 0.25 * (len(v["queries"]) - 1))), reverse=True)
    save = HERE / "out" / "_discover"
    save.mkdir(parents=True, exist_ok=True)
    (save / f"{time.strftime('%Y%m%d-%H%M%S')}-{mode}.json").write_text(
        json.dumps({"mode": mode, "queries": allq, "results": found}, indent=2, ensure_ascii=False))
    return found


def fmt_count(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else f"{n:.0f}"
