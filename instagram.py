"""Post clips to Instagram as Reels with the official Instagram API (Instagram Login / Unified login).

Setup (free, once): a Meta developer app with the Instagram API use case, your professional account added as
an Instagram Tester, then "Generate token" for it in the app's API setup page. Put the token in
clipper/.env as INSTAGRAM_ACCESS_TOKEN=... (it's git-ignored). Run `clipse connect instagram` to check it.

Reels published through the API are PUBLIC immediately; there is no private upload on Instagram.
The video is sent straight from this machine with Meta's resumable upload, so no hosting is needed.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

IG_GRAPH = "https://graph.instagram.com"         # tokens from "Instagram login" (start with IG…)
FB_GRAPH = "https://graph.facebook.com"         # tokens from "Unified login" / system users (start with EAA…)
API_VERSION = os.environ.get("INSTAGRAM_API_VERSION", "v23.0")
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "clipse"
POSTS = CONFIG / "instagram_posts.json"


class NotConnected(RuntimeError):
    pass


def _token() -> str:
    tok = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if not tok:
        raise NotConnected("No INSTAGRAM_ACCESS_TOKEN in clipper/.env — generate one in your Meta app's "
                           "Instagram API setup page (see README → Posting to Instagram).")
    return tok


def _graph() -> str:
    return FB_GRAPH if _token().startswith("EAA") else IG_GRAPH


def _get(path: str, **params) -> dict:
    r = requests.get(f"{_graph()}/{path}", params={**params, "access_token": _token()}, timeout=30)
    data = r.json()
    if "error" in data:
        err = data["error"]
        if err.get("code") == 190:
            raise NotConnected("Instagram token is invalid or expired — generate a new one in the Meta app "
                               "and update INSTAGRAM_ACCESS_TOKEN in .env.")
        raise RuntimeError(f"Instagram API error: {err.get('message')}")
    return data


def _post(path: str, **params) -> dict:
    r = requests.post(f"{_graph()}/{path}", data={**params, "access_token": _token()}, timeout=60)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Instagram API error: {data['error'].get('message')}")
    return data


def user_id() -> str:
    """The Instagram account to post to. Facebook-style tokens need it set: INSTAGRAM_USER_ID in .env."""
    uid = os.environ.get("INSTAGRAM_USER_ID", "").strip()
    if uid:
        return uid
    if _graph() == FB_GRAPH:
        raise NotConnected("Set INSTAGRAM_USER_ID in .env: the numeric ID shown next to your account under "
                           "'Generate access tokens' in the Meta app.")
    me = _get("me", fields="user_id")
    return str(me.get("user_id") or me.get("id"))


def account() -> dict:
    """The connected account: id and username."""
    uid = user_id()
    return {"id": uid, **_get(f"{API_VERSION}/{uid}", fields="username")}


def connected() -> bool:
    return bool(os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip())


# ---------------------------------------------------------------- posting log

def _posts() -> dict:
    return json.loads(POSTS.read_text()) if POSTS.exists() else {}


def posted(clip: Path) -> dict | None:
    return _posts().get(str(Path(clip).resolve()))


def posts_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for p in _posts().values() if p.get("at", "").startswith(today))


def _record(clip: Path, media_id: str, permalink: str | None, speed: float) -> dict:
    CONFIG.mkdir(parents=True, exist_ok=True)
    rec = {"id": media_id, "url": permalink, "speed": speed,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    allp = _posts()
    allp[str(Path(clip).resolve())] = rec
    POSTS.write_text(json.dumps(allp, indent=2))
    return rec


# ---------------------------------------------------------------- caption + upload

def caption(meta: dict, source_url: str | None) -> str:
    lines = [meta.get("title") or "", "", meta.get("hook") or ""]
    if source_url:
        lines += ["", f"Clipped from: {source_url}"]
    lines += ["", "#gaming #gta6 #streamhighlights #reels"]
    return "\n".join(lines).strip()[:2100]          # Instagram's caption limit is 2,200 characters


def upload(clip: Path, meta: dict, source_url: str | None = None, force: bool = False, log=print,
           video: Path | None = None, speed: float = 1.0, share_to_feed: bool = True) -> dict:
    """Publish one clip as a Reel. PUBLIC immediately. Skips clips already posted unless force=True."""
    clip = Path(clip)
    if not force and (prev := posted(clip)):
        log(f"    already on Instagram: {prev['url']}")
        return prev
    uid = user_id()
    f = Path(video or clip)
    # 1. create a Reel container that expects a resumable upload
    container = _post(f"{API_VERSION}/{uid}/media", media_type="REELS", upload_type="resumable",
                      caption=caption(meta, source_url), share_to_feed=str(share_to_feed).lower())
    cid = container["id"]
    # 2. send the file straight from this machine
    size = f.stat().st_size
    with open(f, "rb") as fh:
        r = requests.post(f"{RUPLOAD}/{cid}", data=fh, timeout=600,
                          headers={"Authorization": f"OAuth {_token()}", "offset": "0", "file_size": str(size)})
    if r.status_code >= 300:
        raise RuntimeError(f"Instagram upload failed ({r.status_code}): {r.text[:300]}")
    log(f"    uploaded {size / 1e6:.1f} MB, Instagram is processing…")
    # 3. wait for Instagram to process the video
    for _ in range(90):                      # up to ~7.5 minutes
        st = _get(f"{API_VERSION}/{cid}", fields="status_code,status")
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status')}")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram is still processing after 7 minutes — try `clipse post` again later.")
    # 4. publish
    media_id = _post(f"{API_VERSION}/{uid}/media_publish", creation_id=cid)["id"]
    link = _get(f"{API_VERSION}/{media_id}", fields="permalink").get("permalink")
    rec = _record(clip, media_id, link, speed)
    log(f"    ✓ posted on Instagram (public): {link}")
    return rec
