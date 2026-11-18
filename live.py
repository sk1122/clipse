"""Live mode: watch a stream while it's on air and cut a clip whenever chat explodes.

Two background processes run the whole time:
  * chat    — yt-dlp dumps live chat to a file we tail
  * recorder — yt-dlp | ffmpeg writes the stream into 6-second .ts segments named by wall-clock time
A rolling buffer of segments is kept; when the chat detector fires, we stitch the segments around
the moment, transcribe them, let Claude trim/title it, and render a vertical clip.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import statistics
import subprocess
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import catchup
import picker
import progress
import render
import sources
import transcribe
from signals import HYPE, ChatFilter, channel_names

BUCKET = 5.0
CHAT_LAG = 8.0       # viewers type a few seconds after they see the moment
PRE_ROLL = 50.0      # seconds of video kept before the moment
POST_ROLL = 40.0     # ...and after it (room for the payoff to finish)
MIN_FREE_GB = 2.0    # stop recording if free disk space drops below this


class SpikeDetector:
    """Flags chat bursts relative to this stream's own recent baseline (so busy and quiet chats both work)."""

    def __init__(self, threshold: float = 3.0, warmup: float = 120.0, cooldown: float = 90.0):
        self.counts: dict[int, float] = defaultdict(float)
        self.threshold, self.warmup, self.cooldown = threshold, warmup, cooldown
        self.started = time.time()
        self.next_b: int | None = None
        self.last_fire = 0.0
        self.spike: tuple[int, float, float] | None = None   # (peak bucket, peak score, max z) while spiking
        self.last_z = 0.0
        self.margin = 15.0          # seconds of chat delay to wait for; Live tunes it from measured lag

    def add(self, msg: sources.ChatMsg) -> None:
        self.counts[int(msg.t // BUCKET)] += 1.0 + (1.5 if HYPE.search(msg.text) else 0.0)

    def _score(self, b: int) -> float:
        return sum(self.counts.get(i, 0.0) for i in (b - 2, b - 1, b)) / 3

    def poll(self, now: float) -> list[tuple[float, float]]:
        """Process all fully-arrived buckets; return (wall-clock time, peak spike-score) of finished spikes."""
        done = int((now - self.margin) // BUCKET)   # wait until a bucket's chat has actually arrived
        if self.next_b is None:
            self.next_b = done
        fired = []
        while self.next_b <= done:
            b = self.next_b
            self.next_b += 1
            if (b * BUCKET) - self.started < self.warmup:
                continue
            base = [self._score(i) for i in range(b - 120, b - 12)]
            base = [x for i, x in zip(range(b - 120, b - 12), base) if i * BUCKET >= self.started]
            if len(base) < 12:
                continue
            med = statistics.median(base)
            mad = statistics.median(abs(x - med) for x in base) * 1.4826
            s = self._score(b)
            z = (s - med) / max(mad, 1.0, 0.15 * med)
            self.last_z = z
            if self.spike is None:
                if z >= self.threshold and b * BUCKET - self.last_fire > self.cooldown:
                    self.spike = (b, s, z)
            else:
                pb, ps, pz = self.spike
                if s > ps:
                    self.spike = (b, s, max(pz, z))
                elif z < self.threshold * 0.5 or b - pb >= 6:
                    t = pb * BUCKET + BUCKET / 2
                    fired.append((t, max(pz, z)))
                    self.last_fire = t
                    self.spike = None
        return fired


class Live:
    def __init__(self, a, info: dict, out_dir: Path):
        self.a, self.info, self.url = a, info, a.url
        self.out = out_dir / "live"
        self.segs = self.out / "segments"
        self.work = self.out / "work"
        for d in (self.out, self.segs, self.work):
            d.mkdir(parents=True, exist_ok=True)
        self.chat_file = self.work / "chat"
        self.chat: list[sources.ChatMsg] = []
        self.chat_pos = 0
        self.filter = ChatFilter(channel_names(info))
        self.thresholds = sorted(float(x) for x in str(a.sensitivity).split(","))
        # chat alone may fire a candidate a bit below the lowest threshold; the final score
        # (chat + streamer loudness) decides whether it really becomes a clip
        self.det = SpikeDetector(threshold=self.thresholds[0] * 0.75, cooldown=a.cooldown)
        self.pending: list[tuple[float, float]] = []
        self.loud: dict[int, float] = {}          # wall-clock second -> momentary loudness (LUFS)
        self.loud_done: set[str] = set()
        self.spikes = 0
        self.started = time.time()
        self.procs: list[subprocess.Popen] = []
        self.made = 0
        self.seen = 0
        self.lags: list[float] = []

    # ----- background processes -----
    def _spawn(self, cmd: list[str], **kw) -> subprocess.Popen:
        """Start a child in its own process group, plus a tiny watchdog that kills that group
        if this program dies without cleaning up (Ctrl-C, crash, killed terminal)."""
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
        subprocess.Popen(["sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null && kill -0 {p.pid} 2>/dev/null;"
                          f" do sleep 2; done; kill -TERM -{p.pid} 2>/dev/null"],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def start(self) -> None:
        atexit.register(self.stop)
        log = open(self.work / "procs.log", "a")
        self._spawn(sources.YTDLP + ["--skip-download", "--write-subs", "--sub-langs", "live_chat",
                                     "-o", str(self.chat_file), self.url], stdout=log, stderr=log)
        self.log = log
        self.start_recorder()

    def start_recorder(self) -> None:
        """YouTube live serves video and audio as separate HLS feeds: let ffmpeg read both and
        write 6-second segments named by wall-clock time."""
        q = self.a.live_quality
        urls = sources.run(sources.YTDLP + ["-g", "-f", f"bv*[height<={q}]+ba/b[height<={q}]/b",
                                            self.url]).stdout.split()
        inputs, maps = [], []
        for i, u in enumerate(urls[:2]):
            inputs += ["-i", u]
            maps += ["-map", f"{i}:v?" if i == 0 else f"{i}:a?"] if len(urls) > 1 else ["-map", "0"]
        self.recorder = self._spawn(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", *inputs, *maps, "-c", "copy",
             "-f", "segment", "-segment_time", "6", "-strftime", "1",
             str(self.segs / "%s.ts")],
            stdin=subprocess.DEVNULL, stderr=self.log)
        self.recorder_started = time.time()

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is not None:
                continue
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def free_gb(self) -> float:
        return shutil.disk_usage(self.out).free / 1e9

    # ----- chat -----
    def read_chat(self) -> None:
        path = next((p for p in (self.work / "chat.live_chat.json.part", self.work / "chat.live_chat.json")
                     if p.exists()), None)
        if not path:
            return
        with open(path, "rb") as f:
            f.seek(self.chat_pos)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.chat_pos += end + 1
        now = time.time()
        batch_newest = 0.0
        for line in data[:end].decode("utf-8", "ignore").splitlines():
            for m in sources.parse_live_chat_line(line):
                batch_newest = max(batch_newest, m.t)
                self.seen += 1
                if not self.filter.keep(m):
                    continue
                self.chat.append(m)
                self.det.add(m)
        if batch_newest:
            # how far behind real time chat arrives (varies by stream); score buckets only once complete
            self.lags.append(now - batch_newest)
            self.lags = self.lags[-60:]
            p90 = sorted(self.lags)[int(len(self.lags) * 0.9)]
            self.det.margin = min(60.0, max(10.0, p90 + 4.0))
        cutoff = time.time() - 15 * 60
        if self.chat and self.chat[0].t < cutoff:
            self.chat = [m for m in self.chat if m.t >= cutoff]

    # ----- video buffer -----
    def segments(self) -> list[tuple[float, Path]]:
        out = []
        for p in self.segs.glob("*.ts"):
            try:
                out.append((float(p.stem), p))
            except ValueError:
                pass
        return sorted(out)

    def prune(self) -> None:
        keep_from = time.time() - self.a.buffer_min * 60
        if self.pending:
            keep_from = min(keep_from, min(t for t, _ in self.pending) - CHAT_LAG - PRE_ROLL - 30)
        for t, p in self.segments()[:-2]:
            if t < keep_from:
                p.unlink(missing_ok=True)

    # ----- streamer loudness -----
    def update_loudness(self) -> None:
        """Measure loudness of each newly finished segment (a 6s chunk, so this is cheap)."""
        for t, p in self.segments()[:-1]:
            if p.name in self.loud_done:
                continue
            self.loud_done.add(p.name)
            for rt, m in catchup.loudness(p):
                sec = int(t + rt)
                self.loud[sec] = max(self.loud.get(sec, -70.0), max(m, -70.0))
        cutoff = int(time.time() - 15 * 60)
        for k in [k for k in self.loud if k < cutoff]:
            del self.loud[k]

    def loud_z(self, moment: float) -> float | None:
        """How much louder than usual the stream got around `moment` (robust z-score), or None."""
        def bucket_max(lo, hi):
            vals = [v for k, v in self.loud.items() if lo <= k < hi]
            return max(vals) if vals else None
        base = [bucket_max(b, b + 5) for b in range(int(moment - 600), int(moment - 60), 5)]
        base = [x for x in base if x is not None]
        peak = bucket_max(int(moment - 20), int(moment + 8))
        if len(base) < 24 or peak is None:
            return None
        med = statistics.median(base)
        mad = statistics.median(abs(x - med) for x in base) * 1.4826
        return (peak - med) / max(mad, 1.0)

    def score(self, chat_z: float, moment: float) -> tuple[float, float | None]:
        lz = self.loud_z(moment)
        if lz is None:
            return chat_z, None
        return 0.7 * chat_z + 0.3 * lz, lz

    def window(self, a: float, b: float) -> tuple[Path, float] | None:
        """Stitch segments covering wall-clock [a, b] into one file; returns (file, its start time)."""
        segs = self.segments()
        if not segs or segs[-1][0] < b:      # not recorded yet
            return None
        pick = [(t, p) for i, (t, p) in enumerate(segs)
                if t < b and (i + 1 == len(segs) or segs[i + 1][0] > a)]
        if not pick:
            return None
        # MPEG-TS segments with continuous timestamps can simply be appended byte for byte
        win = self.work / f"window_{int(a)}.ts"
        with open(win, "wb") as out:
            for _, p in pick:
                out.write(p.read_bytes())
        return win, pick[0][0]

    # ----- one clip -----
    def make_clip(self, peak: float, chat_z: float) -> bool:
        moment = peak - CHAT_LAG
        a, b = moment - PRE_ROLL, moment + POST_ROLL
        if not self.segments() or self.segments()[-1][0] < b:
            return False
        self.update_loudness()
        z, lz = self.score(chat_z, moment)
        passes = [t for t in self.thresholds if z >= t]
        stamp = time.strftime("%H:%M:%S", time.localtime(moment))
        detail = f"chat {chat_z:+.1f}, voice/loudness {lz:+.1f}" if lz is not None else f"chat {chat_z:+.1f}"
        if not passes:
            print(f"  · chat bump at {stamp} but score {z:+.1f} ({detail}) is below "
                  f"{self.thresholds[0]} — not clipped")
            self._log({"moment_local_time": stamp, "peak_z": round(z, 2), "chat_z": round(chat_z, 2),
                       "loud_z": None if lz is None else round(lz, 2), "would_clip_at": [], "below_threshold": True})
            progress.emit("spike", moment=stamp, score=round(z, 2), chat_z=round(chat_z, 2),
                          loud_z=None if lz is None else round(lz, 2), passes=[], clipped=False)
            return True
        got = self.window(a, b)
        if not got:
            return False
        win, t0 = got
        win_len = b - t0
        self.spikes += 1
        print(f"\n🔥 spike {z:+.1f} ({detail}) — moment around {stamp} (would clip at threshold "
              f"{', '.join(map(str, passes))}). Processing...")
        progress.emit("spike", moment=stamp, score=round(z, 2), chat_z=round(chat_z, 2),
                      loud_z=None if lz is None else round(lz, 2), passes=passes, clipped=True)

        audio = self.work / "window.wav"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(win),
                        "-vn", "-ac", "1", "-ar", "16000", str(audio)], check=True)
        with catchup.WHISPER_LOCK:
            words = [] if self.a.no_captions and self.a.no_ai else transcribe.transcribe(
                audio, self.a.speech_model)
        transcript = _lines(words)
        chat_sample = [f"[{m.t - t0:5.1f}s] {m.text[:80]}" for m in self.chat if a - 10 <= m.t <= b]
        step = max(1, len(chat_sample) // 40)
        chat_sample = chat_sample[::step][:40]

        clip, skipped = None, None
        if not self.a.no_ai:
            try:
                clip, why = picker.judge_live(transcript, chat_sample, win_len, peak - t0,
                                              self.info.get("title", ""), self.a.min_len, self.a.max_len,
                                              self.a.judge)
                if clip is None:
                    print(f"    Claude would skip it: {why}")
                    skipped = why
                    if not self.a.keep_skipped:
                        self._log({"moment_local_time": stamp, "peak_z": round(z, 2),
                                   "would_clip_at": passes, "claude_skipped": why})
                        progress.emit("clip", moment=stamp, score=round(z, 2), passes=passes,
                                      skipped=why, file=None, part="live")
                        return True
            except Exception as e:
                print(f"    Claude unavailable ({type(e).__name__}) — using the default cut")
        if clip is None:
            s = max(0.0, moment - t0 - 30)
            clip = picker.Clip(s, min(win_len, s + 35), f"Live moment {stamp}", "WAIT FOR IT",
                               skipped or "chat spike", 5)
        clip.start, clip.end = picker.finish_naturally(clip.start, clip.end, words, self.a.min_len,
                                                       self.a.max_len, limit=win_len)

        self.made += 1
        tag = f"z{z:.1f}_" + ("SKIPPED_" if skipped else "")
        name = f"{tag}clip_{time.strftime('%H%M%S', time.localtime(moment))}_{render.slug(clip.title)}.mp4"
        path = render.render_file(win, 0.0, clip, words, self.out / name, self.work / f"cap_{self.made}",
                                  vertical=not self.a.landscape, captions=not self.a.no_captions)
        rec = {**asdict(clip), "moment_local_time": stamp, "peak_z": round(z, 2), "chat_z": round(chat_z, 2),
               "loud_z": None if lz is None else round(lz, 2), "would_clip_at": passes,
               "claude_skipped": skipped, "file": str(path)}
        self._log(rec)
        progress.emit("clip", **rec, score=round(z, 2), passes=passes, skipped=skipped, part="live")
        print(f"    ✓ [{clip.virality}/10] {clip.title}  (spike {z:+.1f})\n      {path}")
        win.unlink(missing_ok=True)
        return True

    def _log(self, rec: dict) -> None:
        with open(self.out / "clips.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ----- main loop -----
    def run(self) -> int:
        print(f"LIVE MODE — {self.info.get('title')}\n"
              f"  recording into a {self.a.buffer_min}-min rolling buffer and watching chat.\n"
              f"  Warming up ~2 min to learn this chat's normal pace. Ctrl-C to stop.\n")
        if self.free_gb() < MIN_FREE_GB + 1:
            print(f"Only {self.free_gb():.1f} GB free on disk — need at least {MIN_FREE_GB + 1} GB. Not starting.")
            return 1
        self.start()
        if not self.a.no_from_start:
            import threading
            def _catch():
                try:
                    catchup.run(self.a, self.info, self.out.parent)
                except Exception as e:
                    print(f"[from start] failed: {type(e).__name__}: {str(e)[:200]}")
            print("  Also clipping the part of the stream that already aired (runs alongside).")
            self.catch_thread = threading.Thread(target=_catch, daemon=True)
            self.catch_thread.start()
        deadline = time.time() + self.a.duration * 60 if self.a.duration else None
        last_status = 0.0
        try:
            while True:
                self.read_chat()
                now = time.time()
                self.pending += self.det.poll(now)
                for p, pz in list(self.pending):
                    if now > p - CHAT_LAG + POST_ROLL + 12:   # wait until the post-roll is recorded
                        try:
                            done = self.make_clip(p, pz)
                        except Exception as e:   # one bad clip must never kill the live session
                            print(f"    ✗ clip failed: {type(e).__name__}: {str(e)[:200]}")
                            done = True
                        if done or now > p + 180:
                            self.pending.remove((p, pz))
                self.update_loudness()
                self.prune()
                if now - getattr(self, "_last_emit", 0) > 5:
                    self._last_emit = now
                    recent = [m for m in self.chat if m.t > now - 60]
                    lz_now = self.loud_z(now - 15)
                    progress.emit("live", elapsed=round(now - self.started), chat_rate=len(recent),
                                  total_seen=self.seen, chat_z=round(self.det.last_z, 2),
                                  loud_z=None if lz_now is None else round(lz_now, 2),
                                  thresholds=self.thresholds, buffer_s=len(self.segments()) * 6,
                                  clips=self.made, spikes=self.spikes, pending=len(self.pending),
                                  warming=(now - self.started) < self.det.warmup + 60,
                                  chat_delay=round(self.det.margin, 1))
                if now - last_status > 30:
                    rate = sum(1 for m in self.chat if m.t > now - 60)
                    print(f"  [{time.strftime('%H:%M:%S')}] chat {rate}/min real (of {self.seen} total)  spike-score {self.det.last_z:+.1f} "
                          f"(fires at {'/'.join(map(str, self.thresholds))})  buffer {len(self.segments())} segs  clips {self.made}")
                    last_status = now
                if self.free_gb() < MIN_FREE_GB:
                    print(f"Disk almost full ({self.free_gb():.1f} GB free) — stopping to protect your Mac.")
                    break
                if self.recorder.poll() is not None:
                    # feed URLs expire / connections drop on long streams: reconnect unless it keeps dying
                    if time.time() - self.recorder_started < 60:
                        print("Recorder stopped (stream ended or can't be read). See work/procs.log.")
                        break
                    print("  recorder dropped — reconnecting...")
                    try:
                        self.start_recorder()
                    except subprocess.CalledProcessError:
                        print("Stream is no longer available (probably ended).")
                        break
                if deadline and now > deadline or (self.a.max_clips and self.made >= self.a.max_clips):
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.stop()
        t = getattr(self, "catch_thread", None)
        if t and t.is_alive():
            print("Live part stopped; waiting for the from-start clips to finish (Ctrl-C to skip)...")
            try:
                t.join()
            except KeyboardInterrupt:
                pass
        if not self.a.keep_buffers:
            shutil.rmtree(self.segs, ignore_errors=True)
            for p in self.work.glob("window*"):
                p.unlink(missing_ok=True)
            for d in self.work.glob("cap_*"):
                shutil.rmtree(d, ignore_errors=True)
        print(f"Made {self.made} clips → {self.out}")
        progress.emit("done", clips=self.made, part="live")
        return 0


def _lines(words: list[sources.Word]) -> str:
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None or w.t - t0 > 6 or len(cur) >= 16:
            if cur:
                lines.append(f"[{t0:5.1f}s] {' '.join(cur)}")
            cur, t0 = [], w.t
        cur.append(w.text)
    if cur:
        lines.append(f"[{t0:5.1f}s] {' '.join(cur)}")
    return "\n".join(lines)
