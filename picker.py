"""Choose the final clips: Claude reads the candidate moments and picks/trims the viral ones.
Falls back to a pure signal-based pick when no Anthropic credentials are available."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from signals import Candidate, fmt
from sources import Word


FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5"}


def _fallback_args(model: str) -> dict:
    """Server-side refusal fallback (re-runs on another model if a safety classifier declines).
    Only some models support it; Haiku has none."""
    if model in FALLBACK_MODELS:
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


LANG_NOTES = {
    "hinglish": ("Write titles and hooks in the stream's own language. For Hindi streams write Hinglish "
                 "(Hindi in Roman script, the way people type it: 'bhai ye kya ho gaya 💀'), never Devanagari."),
    "hindi": ("Write the hook in Hindi using Devanagari script (e.g. 'भाई ये क्या हो गया'); the title may be "
              "Hinglish or Hindi. For non-Hindi streams use the stream's own language."),
    "english": "Write titles and hooks in English, even if the stream is in Hindi or another language.",
    "original": ("Write titles and hooks in the stream's own language and script: English for English "
                 "streams, Devanagari for Hindi streams (e.g. 'भाई ये क्या हो गया')."),
}
CAPTION_LANG = "hinglish"   # set by the CLI (--captions)


class ClipChoice(BaseModel):
    candidate: int = Field(description="Index of the candidate this clip comes from")
    start: float = Field(description="Clip start, absolute seconds from the start of the stream")
    end: float = Field(description="Clip end, absolute seconds from the start of the stream")
    title: str = Field(description="Punchy title for TikTok/Shorts, under 60 characters")
    hook: str = Field(description="On-screen hook text shown at the top, 2-7 words, ALL CAPS ok")
    why: str = Field(description="One sentence: why this moment will perform")
    virality: int = Field(description="1-10 estimate of viral potential")
    category: str = Field(description="'funny' for fails, reactions, chaos, jokes and memes; 'highlight' for "
                                      "impressive, cool, cinematic or hype moments")
    highlight: str = Field(description="The single word of the hook to show big and in colour: the punchline "
                                       "or most surprising word (e.g. 'WWE' in 'THEY GOT WWE IN THE GAME'), "
                                       "never a filler word like THE, GAME, BRO")


class ClipPlan(BaseModel):
    clips: list[ClipChoice]


@dataclass
class Clip:
    start: float
    end: float
    title: str
    hook: str
    why: str
    virality: int
    highlight: str = ""        # word of the hook to show big and in colour (edit styles)
    category: str = ""         # funny | highlight — picks the hashtag set when posting


SYSTEM = """You are a top short-form video editor who turns long livestreams into viral TikToks, \
YouTube Shorts and Reels. You are given candidate moments that a signal detector flagged \
(chat spikes, audience replay peaks, loud audio). Each has a timestamped transcript and a chat sample.

Pick the moments with real viral potential: a strong reaction, a funny line, a fail, a hot take, \
a reveal, drama, or a self-contained story with a payoff. Skip candidates that are just loud \
music, ads/sponsor reads, technical issues, intros/outros, or that need too much context.

For each clip choose precise start/end times (absolute seconds) using the transcript timestamps:
- start right before the setup — no dead air, no mid-word start; the first 2 seconds must hook
- end just after the payoff/reaction lands — at the end of a sentence or once the laugh/reaction finishes, never mid-sentence or mid-word. Prefer slightly longer and complete over short and cut off
- length between {min_len} and {max_len} seconds
Return the clips best-first.

{lang_note}"""


def choose_with_claude(cands: list[Candidate], info: dict, n: int, min_len: int, max_len: int,
                       model: str) -> list[Clip]:
    import anthropic

    blocks = []
    for i, c in enumerate(cands):
        blocks.append(
            f"<candidate index=\"{i}\" peak=\"{c.peak:.0f}s ({fmt(c.peak)})\" signal_score=\"{c.score}\" "
            f"signals=\"{c.signals}\">\n<transcript>\n{c.transcript or '(no transcript)'}\n</transcript>\n"
            f"<chat>\n" + "\n".join(c.chat_sample) + "\n</chat>\n</candidate>")
    user = (f"Stream: {info.get('title')}\nChannel: {info.get('channel') or info.get('uploader')}\n"
            f"Duration: {fmt(info.get('duration') or 0)}\n\n"
            "Transcript timestamps are [h:mm:ss]; convert to absolute seconds for start/end.\n\n"
            + "\n\n".join(blocks)
            + f"\n\nPick up to {n} clips (fewer if fewer are genuinely good).")

    client = anthropic.Anthropic()
    resp = client.beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=SYSTEM.format(min_len=min_len, max_len=max_len, lang_note=LANG_NOTES[CAPTION_LANG]),
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium"},
        output_format=ClipPlan,
        **_fallback_args(model),
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"Claude did not return a plan (stop_reason={resp.stop_reason})")

    clips = []
    for ch in resp.parsed_output.clips[:n]:
        start, end = ch.start, ch.end
        if end - start < min_len:
            end = start + min_len
        if end - start > max_len:
            end = start + max_len
        clips.append(Clip(max(0.0, start), end, ch.title, ch.hook, ch.why, ch.virality, ch.highlight, ch.category))
    return clips


def finish_all(clips: list["Clip"], words: list[Word], min_len: float, max_len: float,
               limit: float | None = None) -> list["Clip"]:
    for c in clips:
        c.start, c.end = finish_naturally(c.start, c.end, words, min_len, max_len, limit)
    return clips


PAUSE = 0.6   # seconds of silence between words that count as a natural break


def finish_naturally(start: float, end: float, words: list[Word], min_len: float, max_len: float,
                     limit: float | None = None) -> tuple[float, float]:
    end = min(end, start + max_len)
    """Move start back to a pause and end forward to the next pause, so a clip never starts or stops
    mid-sentence. The end may grow (up to max_len / `limit`) to let a sentence finish."""
    ws = sorted(words, key=lambda w: w.t)
    start = _snap_start(start, ws)
    grace = 8.0   # a clip may run a few seconds past max_len to finish its sentence
    hard = start + max_len + grace if limit is None else min(start + max_len + grace, limit)
    lo = max(start + min_len, end - 2.0)          # don't end much earlier than asked
    inside = [w for w in ws if lo - 8 <= w.t <= hard + 1]
    if inside and not any(w.t > hard + 1 for w in ws[-1:]):
        inside.append(Word(hard + 99, ""))   # silence after the last word counts as a pause too
    best = None
    for a, b in zip(inside, inside[1:]):
        if b.t - a.t >= PAUSE and a.t >= lo - 0.5 and a.t + 0.5 <= hard:
            best = a.t + 0.5          # first pause after the requested end: let the sentence finish
            break
    if best is None:
        after = [w for w in inside if w.t > end]
        if not after:                 # nobody is talking after the requested end: keep it (+ a beat)
            best = min(end + 0.4, hard)
        else:                         # speech runs on: cut at the biggest gap we can find before the limit
            gaps = [(b.t - a.t, a.t + min(0.5, (b.t - a.t) / 2)) for a, b in zip(inside, inside[1:])
                    if max(start + min_len, end - 8) <= a.t + 0.3 <= hard]
            best = max(gaps)[1] if gaps else min(end, hard)
    end = max(start + min_len, min(best, hard))
    return start, end


def _snap_start(t: float, words: list[Word]) -> float:
    """Move a start time back to the nearest pause in speech so we don't cut mid-sentence."""
    prev = [w for w in words if t - 8 <= w.t <= t]
    for a, b in zip(reversed(prev[:-1]), reversed(prev[1:])):
        if b.t - a.t > 0.7:
            return b.t - 0.15
    return t


def choose_heuristic(cands: list[Candidate], words: list[Word], n: int, min_len: int,
                     max_len: int) -> list[Clip]:
    best = sorted(cands, key=lambda c: c.score, reverse=True)[:n]
    length = max(min_len, min(max_len, 35))   # a target; finish_naturally() moves the end to a pause
    clips = []
    for c in best:
        start = max(0.0, c.peak - length * 0.7)
        clips.append(Clip(start, start + length, f"Moment at {fmt(c.peak)}", "WAIT FOR IT",
                          f"signal spike {c.signals}", min(10, max(1, round(5 + c.score)))))
    return clips


# ---------- live mode: judge one moment at a time ----------

class LiveVerdict(BaseModel):
    keep: bool = Field(description="false if this isn't worth posting (spam, ads, nothing happening)")
    start: float = Field(description="Clip start, seconds from the start of the window")
    end: float = Field(description="Clip end, seconds from the start of the window")
    title: str = Field(description="Punchy title, under 60 characters")
    hook: str = Field(description="On-screen hook text, 2-7 words")
    why: str = Field(description="One sentence: why this will (or won't) perform")
    virality: int = Field(description="1-10 estimate of viral potential")
    category: str = Field(description="'funny' for fails, reactions, chaos, jokes and memes; 'highlight' for "
                                      "impressive, cool, cinematic or hype moments")
    highlight: str = Field(description="The single word of the hook to show big and in colour: the punchline "
                                       "or most surprising word, never a filler word")


LIVE_SYSTEM = SYSTEM.split("For each clip")[0] + """You are watching a stream LIVE. Chat just spiked; \
you get a ~{win:.0f}s recording window around it: the transcript (timestamps in seconds from the \
window start) and chat messages. The chat spike peaked about {peak:.0f}s into the window — the \
moment that caused it is usually a few seconds before that.

Decide if it's worth posting. If yes, choose start/end inside the window: start right before the \
setup (no dead air, no mid-word start), end once the payoff/reaction has fully landed and the sentence is finished — never mid-sentence. {min_len}-{max_len}s long; slightly longer and complete beats cut off.

{lang_note}"""


def judge_live(transcript: str, chat_sample: list[str], window_len: float, peak_at: float,
               stream_title: str, min_len: int, max_len: int, model: str) -> tuple[Clip | None, str]:
    """Returns (Clip on the window timeline or None if Claude says skip, Claude's reason).
    Raises if Claude is unavailable."""
    import anthropic

    user = (f"Stream: {stream_title}\n\n<transcript>\n{transcript or '(no speech detected)'}\n</transcript>\n\n"
            "<chat>\n" + "\n".join(chat_sample) + "\n</chat>")
    resp = anthropic.Anthropic().beta.messages.parse(
        model=model, max_tokens=16000,
        system=LIVE_SYSTEM.format(win=window_len, peak=peak_at, min_len=min_len, max_len=max_len,
                                 lang_note=LANG_NOTES[CAPTION_LANG]),
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium"}, output_format=LiveVerdict,
        **_fallback_args(model),
    )
    v = resp.parsed_output
    if resp.stop_reason == "refusal" or v is None:
        raise RuntimeError(f"no verdict (stop_reason={resp.stop_reason})")
    if not v.keep:
        return None, v.why
    start = max(0.0, min(v.start, window_len - min_len))
    end = min(window_len, max(v.end, start + min_len), start + max_len)
    return Clip(start, end, v.title, v.hook, v.why, v.virality, v.highlight, v.category), v.why
