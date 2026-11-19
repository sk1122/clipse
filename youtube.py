"""Post clips to YouTube (as Shorts) with the official YouTube Data API.

Setup (free, once): a Google Cloud project with "YouTube Data API v3" enabled and an OAuth client of type
"Desktop app", saved as clipper/client_secret.json. Then `clipse connect youtube` signs you in through
Google's own page; the token is saved in ~/.config/clipse/ and never leaves your machine.

Note: Google keeps uploads from unaudited API projects private. Publish them yourself in YouTube Studio,
or apply for Google's (free) API audit to lift that.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIENT_SECRET = HERE / "client_secret.json"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "clipse"
TOKEN = CONFIG / "youtube_token.json"
POSTS = CONFIG / "youtube_posts.json"          # every upload: clip path → video id, time
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
GAMING = "20"                                   # YouTube category id

DEFAULTS = {"privacy": "private", "max_per_day": 5, "shorts_tag": True}


class NotConnected(RuntimeError):
    pass


class LimitReached(RuntimeError):
    """YouTube won't take more uploads today (API quota or the channel's own daily upload limit)."""


# ---------------------------------------------------------------- auth

def connect() -> str:
    """Sign in through Google in the browser and save the token. Returns the channel name."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_SECRET.exists():
        raise NotConnected(f"Missing {CLIENT_SECRET.name}: create a Desktop-app OAuth client in Google Cloud "
                           "and save its JSON there (see README → Posting).")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent",
                                  authorization_prompt_message="Opening Google sign-in in your browser…",
                                  success_message="clipse is connected to YouTube. You can close this tab.")
    _save(creds)
    return channel_name()


def _save(creds) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.exists():
        raise NotConnected("Not connected to YouTube. Run: ./clipse connect youtube")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save(creds)
        else:
            raise NotConnected("YouTube sign-in expired. Run: ./clipse connect youtube")
    return creds


def _api():
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=_creds(), cache_discovery=False)


def connected() -> bool:
    return TOKEN.exists()


def channel_name() -> str:
    r = _api().channels().list(part="snippet", mine=True).execute()
    items = r.get("items") or []
    return items[0]["snippet"]["title"] if items else "(no channel on this account)"


# ---------------------------------------------------------------- posting log

def _posts() -> dict:
    return json.loads(POSTS.read_text()) if POSTS.exists() else {}


def posted(clip: Path) -> dict | None:
    return _posts().get(str(Path(clip).resolve()))


def uploads_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(1 for p in _posts().values() if p.get("at", "").startswith(today))


def _record(clip: Path, video_id: str, privacy: str, speed: float = 1.0) -> dict:
    CONFIG.mkdir(parents=True, exist_ok=True)
    rec = {"id": video_id, "url": f"https://youtube.com/shorts/{video_id}", "privacy": privacy, "speed": speed,
           "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    allp = _posts()
    allp[str(Path(clip).resolve())] = rec
    POSTS.write_text(json.dumps(allp, indent=2))
    return rec


# ---------------------------------------------------------------- metadata + upload

def metadata(meta: dict, source_url: str | None, shorts_tag: bool = True) -> dict:
    title = (meta.get("title") or "Stream highlight").replace("<", "").replace(">", "")
    tag = " #Shorts" if shorts_tag and "#shorts" not in title.lower() else ""
    title = title[: 100 - len(tag)] + tag                  # YouTube's title limit is 100 characters
    lines = [meta.get("hook", "")]
    if source_url:
        lines += ["", f"Clipped from: {source_url}"]
    lines += ["", "#Shorts #gaming" if shorts_tag else "#gaming", "", "Clipped with clipse"]
    return {"title": title, "description": "\n".join(l for l in lines if l is not None)[:4900],
            "tags": ["shorts", "gaming", "stream highlights"]}


def upload(clip: Path, meta: dict, source_url: str | None = None, privacy: str = "private",
           force: bool = False, log=print, video: Path | None = None, speed: float = 1.0) -> dict:
    """Upload one clip. Skips clips already uploaded unless force=True. Returns the post record."""
    from googleapiclient.errors import HttpError, ResumableUploadError
    from googleapiclient.http import MediaFileUpload
    clip = Path(clip)
    if not force and (prev := posted(clip)):
        log(f"    already on YouTube: {prev['url']}")
        return prev
    md = metadata(meta, source_url)
    body = {"snippet": {**md, "categoryId": GAMING},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(video or clip), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    req = _api().videos().insert(part="snippet,status", body=body, media_body=media)
    resp, tries = None, 0
    while resp is None:
        try:
            status, resp = req.next_chunk()
            if status:
                log(f"    uploading… {int(status.progress() * 100)}%")
        except (HttpError, ResumableUploadError) as e:
            body = (getattr(e, "content", b"") or b"").lower()
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1
                time.sleep(2 ** tries)
                continue
            if b"quota" in body:
                raise LimitReached("YouTube's daily API quota is used up — uploads resume tomorrow "
                                   "(it resets at midnight Pacific time).") from e
            if b"exceeded the number of videos" in body or b"uploadlimitexceeded" in body:
                raise LimitReached("YouTube's daily upload limit for this channel is reached — "
                                   "uploads resume tomorrow.") from e
            raise RuntimeError(f"YouTube upload failed ({e.resp.status}): {body[:200].decode(errors='ignore')}") from e
    rec = _record(clip, resp["id"], resp.get("status", {}).get("privacyStatus", privacy), speed)
    log(f"    ✓ uploaded ({rec['privacy']}): {rec['url']}")
    return rec
