"""Post profiles: per-stream hashtags and keywords for captions, chosen by the kind of clip.

A profile is a small JSON file, e.g. profiles/gta6.json:

    {
      "hashtags": {
        "funny":     "#GTA6 #GTAMemes #GTA6Funny #GamingMemes #GamerLife",
        "highlight": "#GTA6 #GTA6Edits #ViceCity #RockstarGames #GamingEdits"
      },
      "keywords": ["GTA 6 gameplay", "GTA 6 funny moments", "..."]
    }

The judge labels each clip "funny" (fails, reactions, chaos, jokes) or "highlight" (cool, impressive or
hype moments). Clips without a label are guessed from their title and description.
YouTube: hashtags go in the description, keywords become the video's tags and a line in the description.
Instagram: hashtags and keywords go at the end of the caption.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

KINDS = ("funny", "highlight")
FUNNY_HINTS = re.compile(
    r"😭|💀|😂|🤣|\b(funny|hilarious|lol|lmao|fail|fails|chaos|chaotic|meme|joke|roast|rage|screams?|"
    r"freaks?\s+out|loses\s+it|weird|weirdest|stupid|dumb|troll|prank|cringe|bro\s|bhai|kya)\b", re.I)


def load(path: str | None) -> dict:
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"Post profile not found: {p}")
    data = json.loads(p.read_text())
    tags = data.get("hashtags", {})
    if isinstance(tags, str):                    # one set for every clip
        tags = {k: tags for k in KINDS}
    kws = data.get("keywords", [])
    if isinstance(kws, str):
        kws = [k.strip() for k in kws.split(",") if k.strip()]
    return {"hashtags": tags, "keywords": kws}


def kind(meta: dict) -> str:
    k = (meta.get("category") or "").lower()
    if k in KINDS:
        return k
    text = " ".join(str(meta.get(f) or "") for f in ("title", "hook", "why"))
    return "funny" if FUNNY_HINTS.search(text) else "highlight"


def hashtags(prof: dict, meta: dict) -> str:
    tags = prof.get("hashtags") or {}
    return tags.get(kind(meta)) or tags.get("highlight") or tags.get("funny") or ""


def keywords(prof: dict) -> list[str]:
    return list(prof.get("keywords") or [])


def youtube_tags(prof: dict, defaults: list[str]) -> list[str]:
    """YouTube allows 500 characters of tags in total."""
    out, total = [], 0
    for t in keywords(prof) + defaults:
        t = t.strip().replace("<", "").replace(">", "")
        if not t or t.lower() in (x.lower() for x in out):
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480:
            break
        out.append(t)
        total += cost
    return out
