"""kattappa TUI — paste a YouTube link, watch the job, review and manage the clips.

Jobs run as separate background processes (they keep going if you close the TUI); the TUI follows
them through each job's out/<id>/job.json + events.jsonl.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, ProgressBar, Select,
                             Sparkline, Static)

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
sys.path.insert(0, str(HERE))
import platform_util  # noqa: E402
from clip import DEFAULT_JUDGE, DEFAULT_SPEECH, JUDGE_MODELS, SPEECH_MODELS  # noqa: E402

REVIEW_STATES = {"new": "·", "keep": "✓ keep", "reject": "✗ reject", "posted": "★ posted"}


# ---------------------------------------------------------------- job model

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class Job:
    """One clipping job, read from its folder."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.meta = json.loads((folder / "job.json").read_text())
        self.events: list[dict] = []
        self._pos = 0
        self.refresh()

    @property
    def id(self) -> str:
        return self.folder.name

    def refresh(self) -> None:
        path = self.folder / "events.jsonl"
        if not path.exists():
            return
        with open(path, "rb") as f:
            f.seek(self._pos)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        self._pos += end + 1
        for line in data[:end].decode("utf-8", "ignore").splitlines():
            try:
                self.events.append(json.loads(line))
            except ValueError:
                pass
        if (self.folder / "job.json").stat().st_mtime > getattr(self, "_meta_t", 0):
            self.meta = json.loads((self.folder / "job.json").read_text())
            self._meta_t = (self.folder / "job.json").stat().st_mtime

    def last(self, kind: str, part: str | None = None) -> dict | None:
        for e in reversed(self.events):
            if e["kind"] == kind and (part is None or e.get("part", "main") == part):
                return e
        return None

    @property
    def live(self) -> bool:
        return self.meta.get("mode") == "live"

    @property
    def status(self) -> str:
        # a folder can hold several runs; only look at events since the latest start
        starts = [i for i, e in enumerate(self.events) if e["kind"] == "start"]
        ev = self.events[starts[-1]:] if starts else self.events
        running = pid_alive(self.meta.get("pid"))
        main_done = any(e["kind"] == "done" and e.get("part", "main") in ("main", "live") for e in ev)
        if any(e["kind"] == "error" for e in ev[-3:]) and not running:
            return "failed"
        if main_done and not running:
            return "done"
        if running:
            return "live ●" if self.live else "running"
        return "stopped"

    def run_events(self) -> list[dict]:
        starts = [i for i, e in enumerate(self.events) if e["kind"] == "start"]
        return self.events[starts[-1]:] if starts else self.events

    def clips(self) -> list[dict]:
        return [e for e in self.events if e["kind"] == "clip"]

    def review(self) -> dict:
        p = self.folder / "review.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def set_review(self, key: str, state: str) -> None:
        r = self.review()
        r[key] = state
        (self.folder / "review.json").write_text(json.dumps(r, indent=2))


def load_jobs() -> dict[str, Job]:
    jobs = {}
    if OUT.exists():
        for f in OUT.iterdir():
            if (f / "job.json").exists():
                try:
                    jobs[f.name] = Job(f)
                except (ValueError, OSError):
                    pass
    return jobs


def fmt_secs(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- dialogs

class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    #box { width: 64; height: auto; padding: 1 2; border: thick $error; background: $surface; }
    #box Horizontal { height: auto; margin-top: 1; align: right middle; }
    #box Button { margin-left: 2; }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Label(self.message)
            with Horizontal():
                yield Button("Cancel", id="no")
                yield Button("Delete", id="yes", variant="error")

    @on(Button.Pressed)
    def done(self, e: Button.Pressed) -> None:
        self.dismiss(e.button.id == "yes")


# ---------------------------------------------------------------- app

class ClipperApp(App):
    TITLE = "kattappa"
    SUB_TITLE = "YouTube livestream → viral clips"
    CSS = """
    #newjob { height: auto; padding: 0 1; border: round $primary; }
    #newjob .row { height: auto; }
    #newjob Input#url { width: 1fr; }
    #newjob Select { width: 22; }
    #newjob Input.small { width: 9; }
    #newjob .lbl { padding: 1 0 0 1; color: $text-muted; }
    #newjob Button { margin-left: 1; }
    #jobs { height: 1fr; }
    #main { height: 1fr; }
    #jobs { width: 44%; border: round $secondary; }
    #right { width: 1fr; }
    #progress { height: auto; max-height: 13; border: round $secondary; padding: 0 1; }
    #progress ProgressBar { margin: 0 0 0 0; }
    #spark { height: 3; }
    #clips { height: 1fr; min-height: 10; border: round $secondary; }
    .muted { color: $text-muted; }
    """
    BINDINGS = [
        Binding("o", "open_clip", "Play"),
        Binding("f", "reveal", "Show file"),
        Binding("k", "review('keep')", "Keep"),
        Binding("x", "review('reject')", "Reject"),
        Binding("p", "review('posted')", "Posted"),
        Binding("d", "delete_clip", "Delete"),
        Binding("h", "hook_clip", "Hook intro"),
        Binding("y", "open_moment", "On YouTube"),
        Binding("s", "stop_job", "Stop job"),
        Binding("tab", "focus_next", "Next pane", show=False),
        Binding("n", "focus_url", "New link"),
        Binding("escape", "leave_input", "Back", show=False),
        Binding("j", "focus_jobs", "Jobs", show=False),
        Binding("c", "focus_clips", "Clips", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.jobs: dict[str, Job] = {}
        self.selected: str | None = None
        self.starting: dict[str, float] = {}   # url -> launch time, until job.json appears
        self._job_sig = self._clip_sig = None

    # ---- layout
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="newjob"):
            with Horizontal(classes="row"):
                yield Input(placeholder="Paste a YouTube link — live or finished — and press Enter", id="url")
                yield Button("Start", id="start", variant="success")
            with Horizontal(classes="row"):
                yield Label("speech", classes="lbl")
                yield Select([(f"{k}", k) for k in SPEECH_MODELS], value=DEFAULT_SPEECH, allow_blank=False,
                             id="speech", tooltip="Speech-to-text model")
                yield Label("judge", classes="lbl")
                yield Select([(k if k != "none" else "no judge", k) for k in JUDGE_MODELS], value=DEFAULT_JUDGE,
                             allow_blank=False, id="judge", tooltip="Claude model that picks and titles clips")
                yield Label("captions", classes="lbl")
                yield Select([("Hinglish", "hinglish"), ("English", "english"),
                              ("As spoken", "original")], value="hinglish", allow_blank=False,
                             id="captions", tooltip="Caption + hook language")
                yield Label("intro", classes="lbl")
                yield Select([("no hook intro", "none"), ("hook intro", "teaser")], value="none",
                             allow_blank=False, id="intro", tooltip="Open each clip with its best moment + a hook line")
                yield Label("clips", classes="lbl")
                yield Input("5", id="nclips", classes="small", tooltip="clips from finished streams / aired part")
                yield Label("threshold", classes="lbl")
                yield Input("2.5", id="sens", classes="small", tooltip="live spike threshold(s), e.g. 2,2.2,2.5")
        with Horizontal(id="main"):
            yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                with Vertical(id="progress"):
                    yield Static("Select or start a job.", id="ptitle")
                    yield Static("", id="pline")
                    yield ProgressBar(total=100, show_eta=False, id="pbar")
                    yield Static("", id="pline2", classes="muted")
                    yield ProgressBar(total=100, show_eta=False, id="pbar2")
                    yield Sparkline([0], id="spark")
                yield DataTable(id="clips", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        jt = self.query_one("#jobs", DataTable)
        jt.add_columns("Status", "Mode", "Channel", "Stream", "Clips")
        ct = self.query_one("#clips", DataTable)
        ct.add_columns("Review", "From", "At", "Score", "Judge", "Title")
        self.query_one("#jobs").border_title = "Jobs  (Enter → clips · n new link · Tab switch)"
        self.query_one("#clips").border_title = "Clips  (Enter play · k keep · x reject · p posted · d delete · y YouTube)"
        self.query_one("#progress").border_title = "Progress"
        self.query_one("#newjob").border_title = "New job"
        self.refresh_all()
        self.set_interval(1.0, self.refresh_all)
        # start on the job list so the keys work straight away; `n` jumps to the link box
        (self.query_one("#jobs", DataTable) if self.jobs else self.query_one("#url", Input)).focus()

    # ---- starting jobs
    @on(Input.Submitted, "#url")
    @on(Button.Pressed, "#start")
    def start_job(self) -> None:
        url = self.query_one("#url", Input).value.strip()
        if not url or ("youtu" not in url and not url.startswith("http")):
            self.notify("Paste a YouTube link first.", severity="warning")
            return
        cmd = [sys.executable, str(HERE / "clip.py"), "run", url,
               "--speech-model", str(self.query_one("#speech", Select).value),
               "--judge", str(self.query_one("#judge", Select).value),
               "--captions", str(self.query_one("#captions", Select).value),
               *(["--hook-intro"] if self.query_one("#intro", Select).value == "teaser" else []),
               "--clips", self.query_one("#nclips", Input).value.strip() or "5",
               "--sensitivity", self.query_one("#sens", Input).value.strip() or "2.5"]
        logs = OUT / "_logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = open(logs / f"{time.strftime('%Y%m%d-%H%M%S')}.log", "w")
        subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(HERE))
        self.starting[url] = time.time()
        self.follow_url = url          # jump to this job as soon as it shows up
        self.query_one("#url", Input).value = ""
        self.query_one("#jobs", DataTable).focus()
        self.notify("Job started — it keeps running even if you close kattappa.", title="Starting")

    # ---- refresh
    def refresh_all(self) -> None:
        fresh = load_jobs()
        for k, j in fresh.items():
            if k in self.jobs:
                self.jobs[k].refresh()
            else:
                self.jobs[k] = j
        for url, t0 in list(self.starting.items()):
            if any(j.meta.get("url") == url for j in self.jobs.values()) or time.time() - t0 > 120:
                del self.starting[url]
        self.render_jobs()
        self.render_progress()
        self.render_clips()

    def ordered_jobs(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.meta.get("started", 0), reverse=True)

    def render_jobs(self) -> None:
        jt = self.query_one("#jobs", DataTable)
        rows = []
        for url in self.starting:
            rows.append((f"start:{url}", ("starting…", "", "", Text(url[-40:], style="dim"), "")))
        for j in self.ordered_jobs():
            st = j.status
            style = {"live ●": "bold red", "running": "bold yellow", "done": "green",
                     "failed": "bold red", "stopped": "dim"}.get(st, "")
            rows.append((j.id, (Text(st, style=style), "LIVE" if j.live else "VOD",
                                (j.meta.get("channel") or "")[:14], (j.meta.get("title") or j.id)[:40],
                                str(sum(1 for c in j.clips() if c.get("file"))))))
        follow = getattr(self, "follow_url", None)
        if follow:
            new = next((j.id for j in self.ordered_jobs() if j.meta.get("url") == follow
                        and time.time() - j.meta.get("started", 0) < 300), None)
            if new:
                self.selected, self.follow_url = new, None
                self._job_sig = self._clip_sig = None
        sig = tuple((k, tuple(str(x) for x in r)) for k, r in rows)
        if sig == self._job_sig:
            return
        self._job_sig = sig
        cur = self.selected
        jt.clear()
        for k, r in rows:
            jt.add_row(*r, key=k)
        keys = [k for k, _ in rows]
        if cur in keys:
            jt.move_cursor(row=keys.index(cur), animate=False)
        elif rows and not cur:
            self.selected = next((k for k in keys if not k.startswith("start:")), None)

    # ---- keyboard navigation (works even where the terminal doesn't pass mouse clicks)
    def action_focus_url(self) -> None:
        self.query_one("#url", Input).focus()

    def action_leave_input(self) -> None:
        self.query_one("#clips", DataTable).focus()

    def action_focus_jobs(self) -> None:
        self.query_one("#jobs", DataTable).focus()

    def action_focus_clips(self) -> None:
        self.query_one("#clips", DataTable).focus()

    @on(DataTable.RowSelected, "#clips")
    def clip_chosen(self) -> None:          # Enter or double-click on a clip plays it
        self.action_open_clip()

    @on(DataTable.RowSelected, "#jobs")
    def job_chosen(self) -> None:           # Enter on a job jumps to its clips
        self.query_one("#clips", DataTable).focus()

    @on(DataTable.RowHighlighted, "#jobs")
    def job_highlighted(self, e: DataTable.RowHighlighted) -> None:
        key = e.row_key.value if e.row_key else None
        if key and not key.startswith("start:") and key != self.selected:
            self.selected = key
            self._clip_sig = None
            self.render_progress()
            self.render_clips()

    def render_progress(self) -> None:
        title, line, line2 = self.query_one("#ptitle", Static), self.query_one("#pline", Static), \
            self.query_one("#pline2", Static)
        bar, bar2, spark = self.query_one("#pbar", ProgressBar), self.query_one("#pbar2", ProgressBar), \
            self.query_one("#spark", Sparkline)
        j = self.jobs.get(self.selected or "")
        if not j:
            title.update("Paste a YouTube link above to start. Live streams are tracked live; "
                         "finished ones show processing progress.")
            for w in (bar, bar2, spark):
                w.display = False
            line.update(""), line2.update("")
            return
        s = j.meta.get("settings", {})
        running = pid_alive(j.meta.get("pid"))
        badge = ("● LIVE  ", "bold red") if j.live and running else \
            (("LIVE (ended)  " if j.live else "VOD  "), "bold magenta" if j.live else "bold cyan")
        conf = (f"speech {s.get('speech_model')} · judge {s.get('judge')} · captions "
                f"{s.get('captions', 'hinglish')} · threshold {s.get('sensitivity')}"
                if s.get("judge") else s.get("note", ""))
        title.update(Text.assemble(badge, (j.meta.get("title") or "", "bold"),
                                   f"\n{j.meta.get('url')}  ·  ", (conf, "dim")))
        ev = j.run_events()
        err = next((e for e in reversed(ev) if e["kind"] == "error"), None)
        if j.live:
            st = next((e for e in reversed(ev) if e["kind"] == "live"), None)
            hist = [e.get("chat_z", 0) for e in ev if e["kind"] == "live"][-90:]
            spark.display = len(hist) >= 3
            spark.data = [max(0.0, h + 3) for h in hist] if hist else [0]
            bar.display = False
            if st:
                lz = st.get("loud_z")
                warm = "  (warming up — learning this chat's normal pace)" if st.get("warming") else ""
                line.update(Text.assemble(
                    ("Live for ", ""), (fmt_secs(st["elapsed"]), "bold"),
                    f"  ·  chat {st['chat_rate']}/min real  ·  spike score ",
                    (f"{st['chat_z']:+.1f}", "bold yellow" if st["chat_z"] >= min(st["thresholds"]) * .75 else "bold"),
                    f" chat / {('-' if lz is None else f'{lz:+.1f}')} voice  ·  clips at "
                    f"{'/'.join(map(str, st['thresholds']))}  ·  spikes {st['spikes']}  ·  clips {st['clips']}"
                    f"  ·  buffer {fmt_secs(st['buffer_s'])}{warm}"))
            else:
                line.update("Connecting to the stream…" if pid_alive(j.meta.get("pid"))
                            else f"Not running ({j.status}).")
            if st and not pid_alive(j.meta.get("pid")):
                line.update(Text.assemble((f"Finished ({j.status}) after {fmt_secs(st['elapsed'])}", "bold"),
                                          f"  ·  {st['spikes']} spikes  ·  {st['clips']} live clips"))
            fs = j.last("stage", "from_start")
            fs_done = any(e["kind"] == "done" and e.get("part") == "from_start" for e in ev)
            if fs and not fs_done:
                bar2.display = True
                pct = ((fs["n"] - 1) + (fs.get("frac") or 0)) / fs["of"] * 100
                bar2.update(progress=pct)
                line2.update(f"Clipping what already aired — step {fs['n']}/{fs['of']}: {fs['name']}"
                             f"{' · ' + fs['detail'] if fs.get('detail') else ''}")
            else:
                bar2.display = False
                n = sum(1 for c in j.clips() if c.get("part") == "from_start" and c.get("file"))
                line2.update(f"Aired part: {n} clips made." if fs_done else
                             ("" if s.get("no_from_start") else "Aired part: waiting to start…"))
        else:
            spark.display = bar2.display = False
            stg = j.last("stage")
            done = any(e["kind"] == "done" for e in ev)
            bar.display = True
            if done:
                bar.update(progress=100)
                line.update(f"Done — {sum(1 for c in j.clips() if c.get('file'))} clips.")
            elif stg:
                bar.update(progress=((stg["n"] - 1) + (stg.get("frac") or 0)) / stg["of"] * 100)
                line.update(f"Step {stg['n']}/{stg['of']}: {stg['name']}"
                            f"{' · ' + stg['detail'] if stg.get('detail') else ''}")
            else:
                bar.update(progress=0)
                line.update("Reading stream info…")
            sig = j.last("signals")
            line2.update(f"Signals: chat {sig['chat']} msgs ({sig['chat_raw'] - sig['chat']} spam removed) · "
                         f"captions {sig['caption_words']} words · most-replayed {'yes' if sig['heatmap'] else 'no'}"
                         if sig else "")
        if err and j.status == "failed":
            line2.update(Text(f"Error: {err['message']}", style="bold red"))

    def render_clips(self) -> None:
        ct = self.query_one("#clips", DataTable)
        j = self.jobs.get(self.selected or "")
        clips = j.clips() if j else []
        review = j.review() if j else {}
        rows = []
        for i, c in enumerate(clips):
            key = c.get("file") or f"skip:{i}"
            exists = bool(c.get("file")) and Path(HERE / c["file"]).exists() or (c.get("file") and Path(c["file"]).exists())
            state = review.get(key, "new")
            part = {"live": "live", "from_start": "aired"}.get(c.get("part", ""), "vod")
            at = c.get("moment_local_time") or c.get("stream_time") or ""
            score = c.get("score", c.get("peak_z"))
            judge = ("skip" if c.get("skipped") or c.get("claude_skipped") else f"{c.get('virality', '-')}/10")
            title = c.get("title") or (c.get("skipped") or "")[:60]
            style = "dim strike" if (c.get("file") and not exists) else ("dim" if judge == "skip" else "")
            rows.append((key, (REVIEW_STATES[state], part, at, "-" if score is None else f"{score:+.1f}",
                               judge, Text(title, style=style))))
        ready = sum(1 for c in clips if c.get("file"))
        ct.border_subtitle = f"🎬 {ready} clips ready" if ready else ""
        sig = (self.selected, tuple((k, tuple(str(x) for x in r)) for k, r in rows))
        if sig == self._clip_sig:
            return
        self._clip_sig = sig
        cur = ct.cursor_row
        ct.clear()
        for k, r in rows:
            ct.add_row(*r, key=k)
        if rows:
            ct.move_cursor(row=min(cur, len(rows) - 1), animate=False)

    # ---- clip actions
    def current_clip(self) -> tuple[Job, dict, str] | None:
        j = self.jobs.get(self.selected or "")
        ct = self.query_one("#clips", DataTable)
        if not j or ct.row_count == 0:
            return None
        key = ct.coordinate_to_cell_key(ct.cursor_coordinate).row_key.value
        clip = next((c for i, c in enumerate(j.clips()) if (c.get("file") or f"skip:{i}") == key), None)
        return (j, clip, key) if clip else None

    def _file(self, clip: dict) -> Path | None:
        if not clip.get("file"):
            return None
        p = Path(clip["file"])
        p = p if p.is_absolute() else HERE / p
        return p if p.exists() else None

    def action_open_clip(self) -> None:
        cur = self.current_clip()
        f = cur and self._file(cur[1])
        if f:
            platform_util.open_path(f)
        else:
            self.notify("No video for this row (judge skipped it, or it was deleted).", severity="warning")

    def action_reveal(self) -> None:
        cur = self.current_clip()
        f = cur and self._file(cur[1])
        j = self.jobs.get(self.selected or "")
        platform_util.open_path(f, reveal=True) if f else platform_util.open_path(j.folder if j else OUT)

    def action_review(self, state: str) -> None:
        cur = self.current_clip()
        if cur:
            j, _, key = cur
            j.set_review(key, "new" if j.review().get(key) == state else state)
            self._clip_sig = None
            self.render_clips()

    def action_delete_clip(self) -> None:
        cur = self.current_clip()
        f = cur and self._file(cur[1])
        if not f:
            self.notify("Nothing to delete for this row.", severity="warning")
            return

        def go(ok: bool | None) -> None:
            if ok:
                f.unlink(missing_ok=True)
                cur[0].set_review(cur[2], "reject")
                self._clip_sig = None
                self.render_clips()
                self.notify(f"Deleted {f.name}")
        self.push_screen(Confirm(f"Delete this clip file? This can't be undone.\n\n{f.name}"), go)

    def action_hook_clip(self) -> None:
        cur = self.current_clip()
        f = cur and self._file(cur[1])
        if not f:
            self.notify("Pick a clip with a video first.", severity="warning")
            return
        judge = str(self.query_one("#judge", Select).value)
        logs = OUT / "_logs"
        logs.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([sys.executable, str(HERE / "clip.py"), "hook", str(f), "--record", "--judge", judge],
                         stdout=open(logs / f"hook-{time.strftime('%H%M%S')}.log", "w"), stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(HERE))
        self.notify("Adding a hook intro — the 🪝 version appears in the list in a few seconds.", title="Hook intro")

    def action_open_moment(self) -> None:
        cur = self.current_clip()
        j = self.jobs.get(self.selected or "")
        if not j:
            return
        url = j.meta.get("url")
        if cur and cur[1].get("url"):
            url = cur[1]["url"]
        platform_util.open_path(url)

    def action_stop_job(self) -> None:
        j = self.jobs.get(self.selected or "")
        pid = j and j.meta.get("pid")
        if pid and pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGINT)   # graceful: finishes, cleans its buffer
            except (ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGINT)
            self.notify("Stopping job — it will finish the clip in progress and clean up.")
        else:
            self.notify("That job isn't running.", severity="warning")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ClipperApp().run()


if __name__ == "__main__":
    main()
