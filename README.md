# clipse

Paste a YouTube link (live or finished) and get vertical, captioned short clips of the moments most likely to go viral. For a live stream it clips while the stream is still on air, and also goes back over the part that already aired.

## Setup

Works on **macOS** and **Linux**. You need Python 3.12 and ffmpeg.

**macOS** (with [Homebrew](https://brew.sh)):

```bash
brew install ffmpeg
```

**Linux** (Debian/Ubuntu; use your distro's package manager otherwise):

```bash
sudo apt install ffmpeg fonts-noto-core fonts-dejavu-core xdg-utils
```

`fonts-noto-core` provides the Devanagari font for Hindi captions. `xdg-utils` lets the TUI open clips and links.

Then, on either system:

```bash
python3 -m venv .venv            # or: uv venv --python 3.12 .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # then add your ANTHROPIC_API_KEY
```

Without an API key everything still runs, but clips are picked from the signals alone, with no AI judge, translation or hook lines.

The first transcription downloads the speech model (about 1.6 GB for the default). Speech runs on the GPU when one is available (Apple silicon on macOS, NVIDIA/CUDA on Linux) and on the CPU otherwise; `--speech-device` overrides this.

Fonts are picked per system: Arial on macOS, and Noto or DejaVu on Linux (any installed font is found through fontconfig).

## Run it

```bash
./clipse                      # TUI: paste a link, pick options, watch progress, review clips
./clipse run <url>            # the same job from the command line
./clipse hook <clip.mp4>      # add a hook intro to a clip you already have
./clipse models               # list the speech and judge models
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
| `u` | upload the selected clip to YouTube (see [Posting](#posting-to-youtube)) |
| `d` | delete a clip file (asks first) |
| `y` / `f` | open that moment on YouTube / show the file in its folder |
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
| `--speed` | play every clip faster, e.g. `1.5` or `2`; the voice keeps its normal pitch | 1 |
| `--hook-intro` | open each clip with its best 2.5 s and a hook line, then the full clip | off |
| `--clips` | clips from a finished stream, or from the already-aired part of a live one | 5 |
| `--sensitivity` | live spike score needed to clip; give several (`2,2.2,2.5`) to compare them in one run | 2.5 |
| `--no-from-start` | live: only clip from now on | off |
| `--keep-skipped` | live: also render moments the judge would skip (`SKIPPED_…`), for tuning | off |
| `--duration`, `--max-clips` | live: stop after N minutes / N clips | until the stream ends |
| `--live-quality` | live recording height | 720 |

## Posting to YouTube

clipse can upload clips to your channel as Shorts through the official YouTube Data API. It's free, and you set it up once:

1. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable **YouTube Data API v3**.
2. On the **OAuth consent screen**, choose **External**, and add the Google account that owns your channel as a **test user**.
3. Under **Credentials**, create an **OAuth client ID** of type **Desktop app**. Download its JSON into this folder as `client_secret.json` (it's git-ignored).
4. Run `./clipse connect youtube`. Google's sign-in page opens in your browser. If your channel is a Brand Account, pick the channel on the account chooser. Google warns that the app is unverified; continue, since it's your own project.

The sign-in token is saved in `~/.config/clipse/` and stays on your machine.

```bash
./clipse post <clip.mp4>               # upload one clip
./clipse post --auto                   # upload every clip you marked keep (k) that isn't uploaded yet
./clipse post --auto --min-score 8     # …plus clips the judge rated 8/10 or higher
./clipse post --auto --dry-run         # preview without uploading
./clipse post --auto --speed 2         # upload 2x-speed copies (your original clips are untouched)
```

In the TUI, `u` uploads the selected clip at the speed picked in the **speed** box, and uploaded clips show `▶yt`.

| Flag | Default |
|---|---|
| `--privacy` | `private`: you publish the clip yourself in YouTube Studio |
| `--max` | 5 uploads per day |

Each upload gets the judge's title plus `#Shorts`, the hook, and a "Clipped from" link to the moment in the stream.

**Limits:**
- **Uploads stay private until Google audits your project.** Google keeps uploads from unaudited API projects private no matter what you choose. Publish them in Studio, or apply for Google's free API audit.
- **Daily quota:** the free API quota allows only a handful of uploads a day.

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
| `clip.py` | CLI entry point (`clipse` wraps it) and the finished-stream pipeline |
| `live.py` | live mode: recorder, chat spike detector, voice loudness, live clips |
| `catchup.py` | clipping the already-aired part of a live stream |
| `sources.py` | yt-dlp: metadata, chat replay, captions, audio |
| `signals.py` | hype curve, candidate moments, chat spam filter |
| `picker.py` | the AI judge, plus natural clip boundaries |
| `transcribe.py` | speech-to-text (faster-whisper) |
| `platform_util.py` | per-OS fonts and opening files/links |
| `hinglish.py` | Hinglish transliteration and English translation of captions |
| `render.py` | vertical render: blurred background, hook banner, captions, fade |
| `hook.py` | hook intros (teaser cold-open + hook line) |
| `speed.py` | speeding clips up with the voice pitch kept |
| `youtube.py` | YouTube sign-in and Shorts uploads |
| `progress.py` | `job.json` / `events.jsonl` progress events that the TUI follows |
| `tui.py` | the terminal UI |

## Output

Each job writes to `out/<video id>/`:
- **Clips:** live clips in `live/`, already-aired clips in `from_start/`.
- **Progress:** `job.json` and `events.jsonl`, which the TUI follows.
- **Review marks:** your keep/reject/posted marks, in `review.json`.

`out/` and `.env` are git-ignored.

## Legal

clipse is a tool for clipping streams **you own or have permission to use**. You are responsible for how you use it.

- **Copyright and creator rights:** clips contain the streamer's footage, voice and likeness. Get the creator's permission before posting clips of their stream, and follow the platform's rules on reposted content.
- **YouTube's Terms of Service:** clipse downloads YouTube content with [yt-dlp](https://github.com/yt-dlp/yt-dlp). It doesn't bypass DRM, paywalls, members-only content or sign-in. Downloading may still be restricted by YouTube's terms, so check they allow your use.
- **AI output:** titles, hooks, translations and clip choices are written by an AI model and can be wrong. Review every clip before you post it.
- **No warranty:** the software is provided as is, under the MIT License (see [LICENSE](LICENSE)).

Not affiliated with or endorsed by YouTube, Google, Anthropic, or any creator.

## Notes

- **Posting other people's streams:** even private uploads can get Content ID claims, and public ones can get copyright strikes (three strikes and the channel is removed). Only post clips you have rights to.
- **API cost:** live mode calls the judge model on every spike that passes the threshold. Choose a cheaper judge, or raise `--sensitivity`, for long streams.

## License

[MIT](LICENSE)
