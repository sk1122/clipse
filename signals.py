"""Turn raw stream data into a per-second 'hype' curve and pick candidate moments."""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field

from sources import ChatMsg, Word

BUCKET = 5.0          # seconds per bucket
CHAT_LAG = 8.0        # chat reacts a few seconds after the moment happens
HYPE = re.compile(
    r"(lmao|lmfao|lol+|kek|omg|wtf|no way|clip (it|that)|\bclip\b|insane|crazy|holy|bruh|"
    r"\bw+\b|\bl+\b|\bdub\b|\bratio\b|aura|😂|🤣|💀|😭|🔥|‼|!!+|\?\?+)", re.I)


@dataclass
class Candidate:
    peak: float                     # seconds, where the signal peaked
    score: float
    signals: dict = field(default_factory=dict)
    transcript: str = ""
    chat_sample: list[str] = field(default_factory=list)


def _robust_z(xs: list[float]) -> list[float]:
    if not xs:
        return xs
    med = statistics.median(xs)
    mad = statistics.median(abs(x - med) for x in xs) * 1.4826
    spread = mad if mad > 1e-6 else (statistics.pstdev(xs) or 1.0)
    return [max(-2.0, min(6.0, (x - med) / spread)) for x in xs]


def _smooth(xs: list[float], k: int) -> list[float]:
    out, n = [], len(xs)
    for i in range(n):
        lo, hi = max(0, i - k), min(n, i + k + 1)
        out.append(sum(xs[lo:hi]) / (hi - lo))
    return out


def hype_curve(duration: float, chat: list[ChatMsg], loud: list[tuple[float, float]],
               heatmap: list[dict] | None) -> tuple[list[float], dict[str, list[float]]]:
    n = max(1, math.ceil(duration / BUCKET))
    raw: dict[str, list[float]] = {}

    if chat:
        rate = [0.0] * n
        for m in chat:
            b = int(max(0.0, m.t - CHAT_LAG) / BUCKET)
            if b < n:
                rate[b] += 1.0 + (1.5 if HYPE.search(m.text) else 0.0)
        raw["chat"] = rate

    if loud:
        lv = [-70.0] * n
        for t, m in loud:
            b = int(t / BUCKET)
            if b < n:
                lv[b] = max(lv[b], max(m, -70.0))
        raw["loudness"] = lv

    if heatmap:
        hv = [0.0] * n
        for seg in heatmap:
            for b in range(int(seg["start_time"] / BUCKET), min(n, int(math.ceil(seg["end_time"] / BUCKET)))):
                hv[b] = max(hv[b], float(seg["value"]))
        raw["replays"] = hv

    weights = {"chat": 0.45, "replays": 0.35, "loudness": 0.20}
    norm = {k: _robust_z(v) for k, v in raw.items()}
    total_w = sum(weights[k] for k in norm) or 1.0
    score = [0.0] * n
    for k, z in norm.items():
        for i, v in enumerate(z):
            score[i] += v * weights[k] / total_w
    return _smooth(score, 3), norm


def pick_candidates(score: list[float], norm: dict[str, list[float]], k: int,
                    min_gap: float = 120.0, skip_intro: float = 60.0, skip_outro: float = 25.0) -> list[Candidate]:
    order = sorted(range(len(score)), key=lambda i: score[i], reverse=True)
    picked: list[Candidate] = []
    end = len(score) * BUCKET
    for i in order:
        t = i * BUCKET + BUCKET / 2
        if t < skip_intro or t > end - skip_outro:    # intros and end screens ("subscribe!") aren't moments
            continue
        if any(abs(t - c.peak) < min_gap for c in picked):
            continue
        picked.append(Candidate(peak=t, score=round(score[i], 2),
                                signals={s: round(v[i], 2) for s, v in norm.items()}))
        if len(picked) >= k:
            break
    return sorted(picked, key=lambda c: c.peak)


def fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def attach_context(cands: list[Candidate], words: list[Word], chat: list[ChatMsg],
                   before: float = 90.0, after: float = 45.0) -> None:
    """Give each candidate a timestamped transcript and a sample of chat around it."""
    for c in cands:
        lo, hi = c.peak - before, c.peak + after
        lines, cur, cur_t = [], [], None
        for w in words:
            if w.t < lo or w.t > hi:
                continue
            if cur_t is None or w.t - cur_t > 6.0 or len(cur) >= 18:
                if cur:
                    lines.append(f"[{fmt(cur_t)}] {' '.join(cur)}")
                cur, cur_t = [], w.t
            cur.append(w.text)
        if cur:
            lines.append(f"[{fmt(cur_t)}] {' '.join(cur)}")
        c.transcript = "\n".join(lines)
        near = [m for m in chat if c.peak - 30 <= m.t <= c.peak + 30]
        step = max(1, len(near) // 20)
        c.chat_sample = [f"[{fmt(m.t)}] {m.text[:80]}" for m in near[::step]][:20]


class ChatFilter:
    """Keeps only chat that looks like a reaction. Drops:
      * bot commands and giveaway entries (/claim, !code)
      * requests aimed at the streamer (code, PS5, shoutout, "mera naam lo", reply/pin me, links)
      * bare greetings ("hi bhai", "hello <channel>", "love from Pune")
      * vote waves and copy-paste: a message that suddenly dominates chat ("timepass"/"mission", "1"/"2")
    Floods of genuine reactions (LMAO, W, 💀, "no way") are never dropped."""

    COMMAND = re.compile(r"^\s*[/!]|\bclaim\b", re.I)
    REQUEST = re.compile(
        r"\b(codes?|ps ?5|giveaway|give ?away|shout ?outs?|s/o|na+m lo|name lo|mera na+m|my name|"
        r"reply (pls|please|me|do|karo|kardo|de)|notice me|pin (me|kar|karo)|heart (do|de|pls|please)|"
        r"subscribe|sub kar|insta(gram)?|follow (me|kar)|membership|https?://|www\.)", re.I)
    GREETING_SRC = (
        r"^\W*(h+i+|hello+|helo|hey+|hlo|hy|namaste|namaskar|salam|ram ram|jai shree ram|gm|gn|"
        r"good (morning|evening|night)|love you|love from [\w ]+|from [\w ]+|"
        r"(i ?a?m )?watching (from|since) [\w ]+|kaise ho|kaisa hai)"
        r"(\W+(bhai|bro|bhaiya|sir|guys|everyone|all|dosto|ji{names}))*\W*$")
    VOTE = re.compile(r"^\W*\d{1,3}\W*$")

    def __init__(self, names: list[str] | None = None, window: float = 120.0, share: float = 0.10, burst_window: float = 20.0,
                 burst_share: float = 0.25):
        from collections import Counter, deque
        extra = "".join("|" + re.escape(n.lower()) for n in (names or []) if len(n) > 2)
        self.GREETING = re.compile(self.GREETING_SRC.replace("{names}", extra), re.I)
        self.window, self.share = window, share
        self.burst_window, self.burst_share = burst_window, burst_share
        self.recent: deque = deque()
        self.counts: Counter = Counter()
        self.burst: deque = deque()
        self.burst_counts: Counter = Counter()

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[\W_]+", "", text.lower())

    def reason(self, msg: ChatMsg) -> str | None:
        """Why this message is noise, or None if it's a real message."""
        text = msg.text
        if self.COMMAND.search(text):
            return "command"
        if self.REQUEST.search(text):
            return "request"
        if self.GREETING.match(text):
            return "greeting"
        if self.VOTE.match(text):
            return "vote"
        key = self._norm(text)
        for q, c, w in ((self.recent, self.counts, self.window), (self.burst, self.burst_counts, self.burst_window)):
            q.append((msg.t, key))
            c[key] += 1
            while q and q[0][0] < msg.t - w:
                _, old = q.popleft()
                c[old] -= 1
        if HYPE.search(text):
            return None
        if len(key) >= 30 and self.counts[key] >= 3:
            return "copy-paste"
        if len(self.recent) >= 30 and key and self.counts[key] / len(self.recent) > self.share:
            return "flood"
        if len(self.burst) >= 15 and key and self.burst_counts[key] / len(self.burst) > self.burst_share:
            return "vote wave"
        return None

    def keep(self, msg: ChatMsg) -> bool:
        return self.reason(msg) is None


def channel_names(info: dict) -> list[str]:
    """Words of the channel name (e.g. 'SomeoneIsLive' → someoneislive, someone), for greeting detection."""
    raw = " ".join(filter(None, [info.get("channel"), info.get("uploader"), info.get("uploader_id")]))
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", raw)}
    words |= {p.lower() for w in re.findall(r"[A-Za-z0-9]+", raw) for p in re.findall(r"[A-Z]?[a-z0-9]+", w)}
    for w in list(words):
        words.add(re.sub(r"(is)?(live|tv|gaming|official|yt)$", "", w))
    return sorted(w for w in words if len(w) > 2 and w not in {"live", "official", "gaming", "tv"})


def filter_chat(msgs: list[ChatMsg], names: list[str] | None = None) -> list[ChatMsg]:
    f = ChatFilter(names)
    return [m for m in sorted(msgs, key=lambda m: m.t) if f.keep(m)]
