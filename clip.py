#!/usr/bin/env python3
"""kattappa — turn YouTube livestreams into viral-ready short clips.

    kattappa                        open the TUI (paste a link, watch progress, manage clips)
    kattappa run <youtube-url>      clip a stream from the command line
    kattappa models                 list the speech and judge models you can pick

A stream that's on air is clipped live (chat + streamer loudness spikes), plus — by default — the part
that already aired. A finished stream/VOD is analysed end to end (chat replay, most-replayed, audio).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SPEECH_MODELS = {
    "tiny": "fastest, rough",
    "base": "fast, rough",
    "small": "fast, OK for English, weak on Hindi",
    "medium": "slower, good multilingual",
    "large-v3-turbo": "best speed/quality balance, good Hindi (default)",
    "large-v3": "most accurate, slowest",
}
JUDGE_MODELS = {
    "claude-opus-5-5": "best judgement (default)",
    "claude-sonnet-5-5": "cheaper, still strong",
    "claude-haiku-5-5": "cheapest and fastest",
    "claude-fable-5-2": "most capable, most expensive",
    "none": "no AI judge — clips are cut purely from the signals",
}
DEFAULT_SPEECH = "large-v3-turbo"
DEFAULT_JUDGE = "claude-opus-5-5"


def load_dotenv() -> None:
    """Read KEY=value lines from clipper/.env (e.g. ANTHROPIC_API_KEY) without overriding the shell."""
    env = HERE / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kattappa", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="open the terminal UI")
    sub.add_parser("models", help="list speech and judge models")
    hk = sub.add_parser("hook", help="add a hook intro (teaser + hook line) to finished clips")
    hk.add_argument("clips", nargs="+", help="clip files")
    hk.add_argument("--text", help="hook line to use (default: written by the judge model)")
    hk.add_argument("--judge", default=DEFAULT_JUDGE, choices=list(JUDGE_MODELS))
    hk.add_argument("--record", action="store_true", help=argparse.SUPPRESS)   # used by the TUI
    r = sub.add_parser("run", help="clip a YouTube stream or video")
    r.add_argument("url", help="YouTube link (live or finished)")

    m = r.add_argument_group("models")
    m.add_argument("--speech-model", default=DEFAULT_SPEECH, choices=list(SPEECH_MODELS),
                   help=f"speech-to-text model for transcripts/captions (default {DEFAULT_SPEECH})")
    m.add_argument("--speech-device", default="auto", choices=["auto", "cpu", "mps"],
                   help="run speech on the Apple GPU (mps) or CPU (default auto)")
    m.add_argument("--judge", default=DEFAULT_JUDGE, choices=list(JUDGE_MODELS),
                   help="Claude model that picks, trims and titles clips, or 'none' (default %(default)s)")
    m.add_argument("--no-judge", action="store_true", help="same as --judge none")

    c = r.add_argument_group("clips")
    c.add_argument("--clips", type=int, default=5, help="clips to make from a finished stream / the aired part (default 5)")
    c.add_argument("--min-len", type=int, default=20, help="min clip length, seconds (default 20)")
    c.add_argument("--max-len", type=int, default=60, help="max clip length, seconds (default 60)")
    c.add_argument("--landscape", action="store_true", help="keep 16:9 instead of 9:16 vertical")
    c.add_argument("--captions", default="hinglish", choices=["hinglish", "english", "original", "hindi"],
                   help="caption + hook language: hinglish (Hindi in Roman script; English stays English), "
                        "english (translated if needed), original/hindi (as spoken — Devanagari for Hindi). "
                        "Default hinglish")
    c.add_argument("--hook-intro", action="store_true",
                   help="open every clip with its best 2.5 s and a hook line, then the full clip")
    c.add_argument("--no-captions", action="store_true")
    c.add_argument("--plan-only", action="store_true", help="finished streams: list moments, don't render")
    c.add_argument("--no-audio", action="store_true", help="finished streams: skip the loudness signal")
    c.add_argument("--out", default=str(HERE / "out"), help="output folder (default clipper/out)")

    lv = r.add_argument_group("live streams")
    lv.add_argument("--sensitivity", default="2.5",
                    help="spike score needed to clip (lower = more clips, default 2.5). Give several, e.g. "
                         "2,2.2,2.5, to test them in one run: each clip is labelled with the ones it passes")
    lv.add_argument("--keep-skipped", action="store_true",
                    help="also render moments the judge would skip (marked SKIPPED_), for tuning")
    lv.add_argument("--cooldown", type=float, default=90, help="min seconds between clips (default 90)")
    lv.add_argument("--buffer-min", type=int, default=10, help="minutes of video kept on disk (default 10)")
    lv.add_argument("--duration", type=float, default=0, help="stop after N minutes (default: until it ends)")
    lv.add_argument("--max-clips", type=int, default=0, help="stop after N live clips")
    lv.add_argument("--no-from-start", action="store_true", help="don't clip the part that already aired")
    lv.add_argument("--live-quality", type=int, default=720, help="recording height (default 720)")
    lv.add_argument("--keep-buffers", action="store_true", help="don't delete the recording buffer at the end")
    # older flag names, still accepted
    r.add_argument("--no-ai", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--model", help=argparse.SUPPRESS)
    r.add_argument("--whisper-model", help=argparse.SUPPRESS)
    return ap


def normalise(a) -> None:
    if a.whisper_model:
        a.speech_model = a.whisper_model
    if a.model:
        a.judge = a.model
    if a.no_judge or a.no_ai:
        a.judge = "none"
    a.no_ai = a.judge == "none"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["tui"]
    elif argv[0].startswith("http") or "youtu" in argv[0]:
        argv = ["run", *argv]          # `kattappa <url>` works too
    a = build_parser().parse_args(argv)

    if a.cmd == "tui":
        import tui
        tui.main()
        return 0
    if a.cmd == "hook":
        return run_hook(a)
    if a.cmd == "models":
        print("Speech models (--speech-model):")
        for k, v in SPEECH_MODELS.items():
            print(f"  {k:16s} {v}")
        print("\nJudge models (--judge):")
        for k, v in JUDGE_MODELS.items():
            print(f"  {k:18s} {v}")
        return 0
    normalise(a)
    return run(a)


def run_hook(a) -> int:
    import time
    import hook
    model = None if a.judge == "none" else a.judge
    for f in a.clips:
        src = Path(f)
        meta = hook.meta_for(src)
        out = hook.add_hook_intro(src, text=a.text, meta=meta, model=model)
        if a.record and meta:      # let the TUI list the hooked version under the same job
            ev = next(p / "events.jsonl" for p in src.resolve().parents if (p / "events.jsonl").exists())
            rec = {**meta, "t": round(time.time(), 2), "kind": "clip", "file": str(out.resolve()),
                   "title": "🪝 " + (meta.get("title") or out.stem), "hooked_from": str(src)}
            with open(ev, "a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"✓ {out}")
    return 0


def run(a) -> int:
    import hinglish
    import picker
    import progress
    import render
    import sources
    import transcribe

    transcribe.DEVICE = a.speech_device
    if a.captions == "hindi":
        a.captions = "original"
    render.CAPTION_LANG = picker.CAPTION_LANG = a.captions
    render.HOOK_INTRO = a.hook_intro
    hinglish.MODEL = None if a.no_ai else a.judge
    if not a.no_ai and not os.environ.get("ANTHROPIC_API_KEY"):
        print("Note: no ANTHROPIC_API_KEY (put it in clipper/.env) — running without the judge.")
        a.no_ai, a.judge, hinglish.MODEL = True, "none", None
    if a.captions == "english" and a.no_ai:
        print("Note: translating captions to English needs the judge model — Hindi speech will show as "
              "Hinglish (English speech is unaffected).")

    out_root = Path(a.out)
    out_root.mkdir(parents=True, exist_ok=True)
    tmp = out_root / "_work_tmp"
    tmp.mkdir(exist_ok=True)
    (tmp / "info.json").unlink(missing_ok=True)   # always re-check: a stream may have gone live/ended

    print("Reading stream info...")
    info = sources.fetch_info(a.url, tmp)
    out_dir = out_root / info["id"]
    is_live = info.get("is_live") or info.get("live_status") == "is_live"
    settings = {k: getattr(a, k) for k in ("speech_model", "judge", "captions", "hook_intro", "clips", "sensitivity", "min_len",
                                           "max_len", "landscape", "no_from_start", "duration")}
    progress.setup(out_dir, a.url, info, "live" if is_live else "vod", settings)
    (tmp / "info.json").unlink(missing_ok=True)

    if is_live:
        import live
        return live.Live(a, info, out_dir).run()

    try:
        return run_vod(a, info, out_dir)
    except Exception as e:
        progress.emit("error", message=f"{type(e).__name__}: {str(e)[:300]}")
        raise


def run_vod(a, info: dict, out_dir: Path) -> int:
    import picker
    import progress
    import render
    import signals
    import sources
    import transcribe

    S = lambda n, name, detail="", frac=None: progress.stage(n, 5, name, detail, frac=frac)
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "info.json").write_text(json.dumps(info))
    duration = float(info.get("duration") or 0)
    print(f"    {info.get('title')}  [{signals.fmt(duration)}]")

    print("1/5 Collecting signals (chat, captions, replays, audio)...")
    S(1, "Collecting signals", "chat replay")
    raw_chat = sources.fetch_chat(a.url, info, work)
    chat = signals.filter_chat(raw_chat, signals.channel_names(info))
    if raw_chat:
        print(f"    ignored {len(raw_chat) - len(chat)} spam/command/request messages")
    S(1, "Collecting signals", "captions", 0.4)
    words = sources.fetch_words(a.url, info, work)
    heat = info.get("heatmap") or []
    S(1, "Collecting signals", "audio loudness", 0.6)
    loud = [] if a.no_audio else sources.fetch_loudness(a.url, info, work)
    print(f"    chat msgs: {len(chat)}  caption words: {len(words)}  "
          f"replay heatmap: {'yes' if heat else 'no'}  loudness frames: {len(loud)}")
    progress.emit("signals", chat=len(chat), chat_raw=len(raw_chat), caption_words=len(words),
                  heatmap=bool(heat), loudness=bool(loud))
    if not (chat or heat or loud):
        print("No usable signals for this video (no chat replay, heatmap or audio).")
        progress.emit("error", message="no usable signals (no chat replay, heatmap or audio)")
        return 1

    print("2/5 Finding hype peaks...")
    S(2, "Finding hype peaks")
    score, norm = signals.hype_curve(duration, chat, loud, heat)
    n_cands = min(18, max(a.clips * 3, 8))
    cands = signals.pick_candidates(score, norm, n_cands,
                                    min_gap=min(120.0, max(30.0, duration / (n_cands * 2))),
                                    skip_intro=60.0 if duration > 600 else 0.0)

    if not words and not a.no_audio:   # no YouTube captions: transcribe just the candidate moments
        print("3/5 No captions on YouTube — transcribing the candidate moments...")
        audio = next(iter(work.glob("audio.*")), None)
        for ci, c in enumerate(cands):
            S(3, "Transcribing", f"{ci + 1}/{len(cands)}", ci / max(1, len(cands)))
            lo = max(0.0, c.peak - 90)
            seg = work / "seg.wav"
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{lo:.2f}", "-t", "135",
                            "-i", str(audio), "-ac", "1", "-ar", "16000", str(seg)], check=True)
            words += [sources.Word(w.t + lo, w.text) for w in transcribe.transcribe(seg, a.speech_model)]
        words.sort(key=lambda w: w.t)
    signals.attach_context(cands, words, chat)
    print(f"    {len(cands)} candidate moments")

    print("4/5 Choosing the best clips...")
    S(4, "Choosing clips", f"{a.judge} is judging" if not a.no_ai else "signal-only picks")
    clips = None
    if not a.no_ai:
        try:
            clips = picker.choose_with_claude(cands, info, a.clips, a.min_len, a.max_len, a.judge)
            print(f"    {a.judge} picked {len(clips)} clips")
        except Exception as e:  # network, refusal: still produce clips
            print(f"    judge unavailable ({type(e).__name__}: {str(e)[:160]}) — using signal-only picks")
    if not clips:
        clips = picker.choose_heuristic(cands, words, a.clips, a.min_len, a.max_len)
    picker.finish_all(clips, words, a.min_len, a.max_len, limit=duration)
    clips = [c for c in clips if c.start < duration]
    for c in clips:
        c.end = min(c.end, duration)

    results = []
    for i, c in enumerate(clips, 1):
        print(f"    #{i} {signals.fmt(c.start)}-{signals.fmt(c.end)}  [{c.virality}/10] {c.title}")
        results.append({**asdict(c), "stream_time": signals.fmt(c.start),
                        "url": f"https://youtu.be/{info['id']}?t={int(c.start)}"})

    made = 0
    if not a.plan_only:
        print("5/5 Rendering clips...")
        for i, (c, r) in enumerate(zip(clips, results), 1):
            S(5, "Rendering", f"clip {i}/{len(clips)}", (i - 1) / max(1, len(clips)))
            try:
                path = render.render(a.url, c, words, i, out_dir, work,
                                     vertical=not a.landscape, captions=not a.no_captions)
                r["file"] = str(path)
                made += 1
                progress.emit("clip", **r)
                print(f"    ✓ {path}")
            except Exception as e:   # one failed clip (download error, render timeout) shouldn't stop the rest
                msg = getattr(e, "stderr", None) or str(e)
                print(f"    ✗ clip {i} failed: {type(e).__name__}: {str(msg)[-300:]}")
                progress.emit("clip_failed", n=i, message=f"{type(e).__name__}: {str(msg)[-200:]}")

    (out_dir / "clips.json").write_text(json.dumps(
        {"video": info.get("title"), "id": info["id"], "clips": results}, indent=2, ensure_ascii=False))
    progress.emit("done", clips=made)
    print(f"\nDone → {out_dir}/clips.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
