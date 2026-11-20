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
./clipse restart <link>       # stop a live job and start it again with the same settings
./clipse restart <link> --clips 25 --style split   # …or with some settings changed
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
| `r` | restart the selected live job with the same settings (it stops cleanly first; the already-aired part isn't clipped twice) |
| `q` | quit (jobs keep running in the background) |

For a live stream, the progress panel shows chat rate, chat and voice spike scores and clips made. For a finished stream it shows which processing step it's on.

### Options

The TUI offers the same choices next to the link box.

| Flag | Choices | Default |
|---|---|---|
| `--speech-model` | tiny, base, small, medium, large-v3-turbo, large-v3 | large-v3-turbo |
| `--judge` | claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5, claude-fable-5-2, none | claude-opus-5-5 |
| `--captions` | hinglish (Hindi in Roman script), english (translated if needed), original (as spoken; `hindi` = Devanagari) | hinglish |
| `--post-to` | `youtube`, `instagram` or `both`: upload every clip the moment it's made. YouTube uploads are private; Instagram Reels are public. Clips the judge skips are never posted | none |
| `--post-max` | max auto-posts per platform per day | 5 |
| `--speed` | play every clip faster, e.g. `1.5` or `2`; the voice keeps its normal pitch | 1 |
| `--hook-intro` | open each clip with its best 2.5 s and a hook line, then the full clip | off |
| `--clips` | clips from a finished stream, or from the already-aired part of a live one | 5 |
| `--sensitivity` | live spike score needed to clip; give several (`2,2.2,2.5`) to compare them in one run | 2.5 |
| `--no-from-start` | live: only clip from now on | off |
| `--keep-skipped` | live: also render moments the judge would skip (`SKIPPED_…`), for tuning | off |
| `--duration`, `--max-clips` | live: stop after N minutes / N clips | until the stream ends |
| `--live-quality` | live recording height | 720 |

## Finding videos to clip

`clipse discover` searches YouTube for your queries and lists the best new videos. Videos clipse has already clipped are skipped.

```bash
./clipse discover "gta 6 funny moments" "gta 6 rage" --mode live --expand 10
./clipse discover --queries-file queries.txt --mode popular --top 30
./clipse discover --queries-file queries.txt --mode live --run 3 --with --clips 25 --style split --speed 1.5
```

| Option | Does | Default |
|---|---|---|
| `--mode` | `live`: streams on air now, ranked by live viewers · `week`: uploaded this week, ranked by views per day · `popular`: ranked by total views | live |
| `--expand N` | the judge model writes N more queries in the same spirit, on the same topic | 0 |
| `--run N` | start clipping jobs on the top N; flags after `--with` go to each job | off |
| `--min-minutes` | skip finished videos shorter than this | 2 |

**Filters:**
- **Relevance:** results must match the query's distinctive words; numbers must match exactly, so "GTA 6" won't return GTA 5.
- **Not real moments:** 24/7 reruns, "best of" loops, news and music streams are dropped, as are Shorts and finished videos with very few views.

Every search is saved to `out/_discover/`.

## Edit styles

Pick a style in the TUI (**style**) or with `--style`:

| Style | Looks like |
|---|---|
| `split` | content on top, the streamer's webcam big underneath, a big title across the middle with the punchline word in red |
| `fullscreen` | content fills the frame, with a webcam bubble in a corner |
| `reaction` | the webcam fills most of the frame, with the content in a strip below |
| `classic` | 16:9 video on a blurred background, with the hook banner on top |
| `auto` (default) | `split` when the stream has a webcam overlay, `fullscreen` when the camera is the whole picture (IRL, just chatting, vlogs), `classic` when there's no camera |

**How the webcam is found:**
- **Overlays:** clipse detects the streamer's face and the fixed rectangle around it, in any corner, size or shape. Faces that never move (posters, menu art) are ignored.
- **Per-stream memory:** a webcam doesn't move during a stream, so clipse remembers where it was found and goes with the stream's majority. One confusing clip can't move the crop.
- **Split layout:** the top half shows the side of the picture away from the webcam, so the cam isn't shown twice.

**Extras** (all optional):

| Option | Does | Default |
|---|---|---|
| `--karaoke` | captions 2–3 words at a time, with the spoken word lit up | on |
| `--zoom-punch` | quick zoom-in on the loudest moment | on |
| `--filter` | colour look for the footage: `vivid`, `cinematic`, `warm`, `cool`, `bw`, `vintage`, `dramatic` | none |
| `--music` | low-volume background music that dips whenever someone talks. `auto` generates an original high-energy track per clip (no copyright issues); or give a music file or folder. The TUI's "my music folder" uses `clipper/music/` | none |
| `--music-volume` | music level, 0–1 | 0.4 |

The judge also picks which word of the hook to highlight.

## Posting to YouTube

**Hashtags and keywords:** add a post profile, `--post-profile profiles/gta6.json` (or `--profile` with `clipse post`). It's a small JSON file with two hashtag sets and a keyword list:

```json
{
  "hashtags": {"funny": "#GTA6 #GTAMemes ...", "highlight": "#GTA6 #GTA6Edits ..."},
  "keywords": ["GTA 6 gameplay", "GTA 6 funny moments", "..."]
}
```

- **Which hashtag set:** the judge labels each clip **funny** (fails, reactions, chaos) or **highlight** (cool, impressive or hype moments), and the matching set is used.
- **Keywords:** they become YouTube tags, and go at the end of the description or caption on both platforms.
- **Example:** see `profiles/gta6.json`.

**Auto-post while clipping:** in the TUI, pick **post to** next to the link (or pass `--post-to youtube|instagram|both`), and every clip is uploaded as soon as it's made. Uploads run in the background, so they never slow down live clipping. The clip list marks posted clips with `▶yt` and `▶ig`, and the progress panel shows what's been posted. Connect each platform first (below).

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

## Posting to Instagram

clipse can publish clips as Reels through the official Instagram API. It's free, and the video goes straight from your machine, so no hosting is needed. You set it up once:

1. Make your Instagram account a **Creator** or **Business** account (Instagram app → Settings → Account type and tools).
2. At [developers.facebook.com](https://developers.facebook.com), create an app and pick the use case **Manage messaging & content on Instagram**.
3. **Use cases → Customize → Permissions and features:** add `instagram_business_basic` and `instagram_business_content_publish`.
4. **API setup:** create the login configuration.
5. **App roles → Roles:** add your account as an **Instagram Tester**, and accept the invite in Instagram (Settings → Website permissions → Apps and websites → Tester invites).
6. Under **Generate access tokens**, click **Generate token** for your account. Add the token to `.env`, plus your account's numeric ID (shown next to it):
   ```
   INSTAGRAM_ACCESS_TOKEN=...
   INSTAGRAM_USER_ID=...
   ```
7. Run `./clipse connect instagram` to check it.

```bash
./clipse post <clip.mp4> --to instagram             # one clip
./clipse post --auto --to instagram                 # every clip you marked keep (k) not posted yet
./clipse post --auto --to both --speed 1.5          # YouTube and Instagram, at 1.5x
```

**Reels are public as soon as they're posted;** Instagram has no private uploads. So `--auto` only posts clips you marked **keep**, never clips chosen by judge score alone. Instagram allows 100 API posts per account per day.

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
| `facecam.py` | finding the streamer's webcam in any layout |
| `styles.py` | split / fullscreen / reaction layouts and the big title |
| `music.py` | generated background music |
| `speed.py` | speeding clips up with the voice pitch kept |
| `youtube.py` | YouTube sign-in and Shorts uploads |
| `instagram.py` | Instagram Reels publishing |
| `autopost.py` | uploading clips in the background as a job makes them |
| `discover.py` | searching YouTube for videos to clip |
| `postprofile.py` | hashtags and keywords per stream, picked by clip type |
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

- **Posting other people's streams:** even private uploads can get Content ID claims, and public ones can get copyright strikes (three strikes and the channel is removed). Instagram can remove reposted content and restrict accounts that get repeated reports. Only post clips you have rights to.
- **API cost:** live mode calls the judge model on every spike that passes the threshold. Choose a cheaper judge, or raise `--sensitivity`, for long streams.

## License

[MIT](LICENSE)
