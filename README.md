# kattappa

Paste a YouTube link (live or finished) and get vertical, captioned short clips of the moments most likely to go viral. For a live stream it clips while the stream is still on air, and also goes back over the part that already aired.

## Setup

macOS with [Homebrew](https://brew.sh) and [uv](https://docs.astral.sh/uv/):

```bash
brew install ffmpeg
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env        # then add your ANTHROPIC_API_KEY
```

Without an API key everything still runs, but clips are picked from the signals alone, with no AI judge, translation or hook lines.

The first transcription downloads the speech model (about 1.6 GB for the default).

## Run it

```bash
./kattappa                    # TUI: paste a link, pick options, watch progress, review clips
./kattappa run <url>          # the same job from the command line
./kattappa hook <clip.mp4>    # add a hook intro to a clip you already have
./kattappa models             # list the speech and judge models
```

Run the TUI in a normal terminal (Terminal, iTerm, …). Some embedded terminal panels don't pass mouse clicks through, but everything also works from the keyboard.

### TUI keys

| Key | Does |
|---|---|
| `n` / `Esc` | type a new link / leave the link box |
| `↑` `↓`, `Tab` | move through jobs and clips, switch pane |
| `Enter` | on a job: jump to its clips · on a clip: play it |
| `k` / `x` / `p` | mark a clip keep / reject / posted |
| `h` | add a hook intro to the selected clip (adds a 🪝 version) |
| `d` | delete a clip file (asks first) |
| `y` / `f` | open that moment on YouTube / show the file in Finder |
| `s` | stop the selected job |
| `q` | quit (jobs keep running in the background) |

For a live stream, the progress panel shows chat rate, chat and voice spike scores and clips made. For a finished stream it shows which processing step it's on.

### Options

The TUI offers the same choices next to the link box.

| Flag | Choices | Default |
|---|---|---|
| `--speech-model` | tiny, base, small, medium, large-v3-turbo, large-v3 | large-v3-turbo |
| `--judge` | claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5, claude-fable-5-2, none | claude-opus-5-5 |
| `--captions` | hinglish (Hindi in Roman script), english (translated if needed), original (as spoken; `hindi` = Devanagari) | hinglish |
| `--hook-intro` | open each clip with its best 2.5 s and a hook line, then the full clip | off |
| `--clips` | clips from a finished stream, or from the already-aired part of a live one | 5 |
| `--sensitivity` | live spike score needed to clip; give several (`2,2.2,2.5`) to compare them in one run | 2.5 |
| `--no-from-start` | live: only clip from now on | off |
| `--keep-skipped` | live: also render moments the judge would skip (`SKIPPED_…`), for tuning | off |
| `--duration`, `--max-clips` | live: stop after N minutes / N clips | until the stream ends |
| `--live-quality` | live recording height | 720 |

## How it works

- **Live:**
  - **Chat:** real chat activity counts. Bot commands, code/PS5/shoutout requests, greetings, vote waves and copy-paste are filtered out.
  - **Scoring:** chat is combined with how much louder the streamer got (70% chat, 30% voice). Chat delay is measured for each stream, so a spike is only scored once its chat has arrived.
  - **Recording:** clips are cut from a rolling 10-minute buffer, which is deleted when the run ends.
- **Already-aired part of a live stream:** downloaded from YouTube's DVR in numbered chunks (their length is read for each stream), ranked by loudness plus transcript. Each clip is then captioned from its own audio.
- **Finished streams:** chat replay, YouTube's "most replayed" data and loudness.

The judge reads the transcript and chat, decides whether a moment is worth posting, trims it to 20–60 s and writes a title and hook. Each clip's start and end are then moved to pauses in speech, so nothing starts or stops mid-sentence (a clip may run a few seconds over to finish its sentence). Every clip ends with a short fade.

## Layout

| File | What it does |
|---|---|
| `clip.py` | CLI entry point (`kattappa` wraps it) and the finished-stream pipeline |
| `live.py` | live mode: recorder, chat spike detector, voice loudness, live clips |
| `catchup.py` | clipping the already-aired part of a live stream |
| `sources.py` | yt-dlp: metadata, chat replay, captions, audio |
| `signals.py` | hype curve, candidate moments, chat spam filter |
| `picker.py` | the AI judge, plus natural clip boundaries |
| `transcribe.py` | speech-to-text (faster-whisper) |
| `hinglish.py` | Hinglish transliteration and English translation of captions |
| `render.py` | vertical render: blurred background, hook banner, captions, fade |
| `hook.py` | hook intros (teaser cold-open + hook line) |
| `progress.py` | `job.json` / `events.jsonl` progress events that the TUI follows |
| `tui.py` | the terminal UI |

## Output

Each job writes to `out/<video id>/`:
- **Clips:** live clips in `live/`, already-aired clips in `from_start/`.
- **Progress:** `job.json` and `events.jsonl`, which the TUI follows.
- **Review marks:** your keep/reject/posted marks, in `review.json`.

`out/` and `.env` are git-ignored.

## Notes

- **Footage rights:** clips contain the streamer's footage and audio. Check the creator's permission and the platform's rules before posting them.
- **API cost:** live mode calls the judge model on every spike that passes the threshold. Choose a cheaper judge, or raise `--sensitivity`, for long streams.
