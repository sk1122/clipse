"""An original high-energy EDM track, synthesised from scratch (no samples, so no licensing questions).
140 BPM in A minor (Am–F–C–G). It builds until `drop` seconds in, then hits full energy."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 44100
BPM = 140
BEAT = 60 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(7)

# A minor progression, one chord per bar: (bass root Hz, chord notes Hz)
NOTE = lambda n: 440.0 * 2 ** ((n - 69) / 12)   # MIDI note → Hz
PROG = [(NOTE(45), [NOTE(69), NOTE(72), NOTE(76)]),    # Am
        (NOTE(41), [NOTE(65), NOTE(69), NOTE(72)]),    # F
        (NOTE(48), [NOTE(67), NOTE(72), NOTE(76)]),    # C
        (NOTE(43), [NOTE(67), NOTE(71), NOTE(74)])]    # G


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    X *= 1 / np.sqrt(1 + (f / cutoff) ** 4)
    return np.fft.irfft(X, len(x))


def highpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    return x - lowpass(x, cutoff)


def saw(freq: float, n: int, detune: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    ph = (t * freq * (1 + detune) + rng.random()) % 1.0
    return 2 * ph - 1


def env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-np.maximum(t - attack, 0) / decay)


def kick() -> np.ndarray:
    n = int(0.42 * SR)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t / 0.035)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.22)
    click = rng.standard_normal(n) * np.exp(-t / 0.004) * 0.4
    return np.tanh(2.2 * (body + click))


def clap() -> np.ndarray:
    n = int(0.25 * SR)
    noise = highpass(rng.standard_normal(n), 900)
    e = np.zeros(n)
    for d in (0.0, 0.011, 0.022):                       # a clap is a few quick bursts
        i = int(d * SR)
        e[i:] += np.exp(-np.arange(n - i) / SR / 0.06)
    return noise * e * 0.5


def hat(open_: bool = False) -> np.ndarray:
    n = int((0.22 if open_ else 0.05) * SR)
    return highpass(rng.standard_normal(n), 7000) * env(n, 0.001, 0.08 if open_ else 0.015) * 0.35


def place(track: np.ndarray, sound: np.ndarray, t: float, gain: float = 1.0) -> None:
    i = int(t * SR)
    if i >= len(track) or i < 0:
        return
    j = min(len(track), i + len(sound))
    track[i:j] += sound[: j - i] * gain


def make_track(duration: float, drop: float, out: Path) -> Path:
    n = int((duration + 1) * SR)
    drums, bass, chords, fx = (np.zeros(n) for _ in range(4))
    side = np.ones(n)                                    # sidechain: everything ducks under the kick
    K, C, H, HO = kick(), clap(), hat(), hat(True)
    duck = 1 - 0.75 * np.exp(-np.arange(int(0.3 * SR)) / SR / 0.07)

    # bars are aligned so a bar starts exactly on the drop
    first_bar = drop - np.ceil(drop / BAR) * BAR
    bar_t = first_bar
    bar_i = 0
    while bar_t < duration:
        root, notes = PROG[bar_i % 4]
        building = bar_t < drop
        bars_to_drop = (drop - bar_t) / BAR
        for b in range(4):
            bt = bar_t + b * BEAT
            if bt < 0:
                continue
            if not building:
                place(drums, K, bt, 1.0)
                i = int(bt * SR)
                side[i:i + len(duck)] = np.minimum(side[i:i + len(duck)], duck[: max(0, min(len(duck), n - i))])
                if b in (1, 3):
                    place(drums, C, bt, 0.9)
                place(drums, HO, bt + BEAT / 2, 0.8)
                place(drums, H, bt + BEAT / 4, 0.5)
                place(drums, H, bt + 3 * BEAT / 4, 0.5)
            else:
                # build: hats get busier and a clap roll speeds up into the drop
                place(drums, H, bt + BEAT / 2, 0.35 + 0.3 * (1 - min(bars_to_drop, 4) / 4))
                if bars_to_drop <= 2:
                    div = 4 if bars_to_drop > 1 else 8
                    for k in range(div // 2 if bars_to_drop > 1 else div):
                        frac = k / (div // 2 if bars_to_drop > 1 else div)
                        place(drums, C, bt + frac * BEAT, 0.25 + 0.5 * (1 - bars_to_drop / 2))
                else:
                    place(drums, K, bt, 0.6)         # four-on-the-floor from the start: energy right away
                    i = int(bt * SR)
                    side[i:i + len(duck)] = np.minimum(side[i:i + len(duck)],
                                                       0.5 + 0.5 * duck[: max(0, min(len(duck), n - i))])
        # bass: driving off-beat eighths after the drop, sustained rumble before
        i0, i1 = int(max(bar_t, 0) * SR), int(min(bar_t + BAR, duration + 1) * SR)
        if i1 > i0:
            seg = i1 - i0
            if not building:
                for k in range(8):
                    t8 = bar_t + k * BEAT / 2
                    s = int(t8 * SR)
                    if s < 0 or s >= n:
                        continue
                    ln = min(int(BEAT / 2 * SR * 0.9), n - s)
                    tone = lowpass(saw(root, ln) + 0.5 * saw(root * 2, ln, 0.003), 420) * env(ln, 0.004, 0.18)
                    bass[s:s + ln] += tone * 0.55
                # supersaw stabs on the off-beats
                for k in range(8):
                    if k % 2 == 0:
                        continue
                    s = int((bar_t + k * BEAT / 2) * SR)
                    ln = min(int(0.2 * SR), n - s) if s < n else 0
                    if ln <= 0:
                        continue
                    stab = sum(saw(f, ln, d) for f in notes for d in (-0.008, 0.0, 0.009)) / 9
                    chords[s:s + ln] += lowpass(stab, 5200) * env(ln, 0.003, 0.09) * 0.55
            else:
                pad = sum(saw(f, seg, d) for f in notes for d in (-0.006, 0.006)) / 6
                openness = 1 - min(bars_to_drop, 8) / 8          # filter opens as the drop approaches
                chords[i0:i1] += lowpass(pad, 500 + 3500 * openness ** 2) * (0.25 + 0.25 * openness)
                bass[i0:i1] += lowpass(saw(root, seg), 200) * 0.18
        bar_t += BAR
        bar_i += 1

    # riser: a noise sweep + rising tone over the 4 bars before the drop, then an impact
    r0 = max(0.0, drop - 4 * BAR)
    rn = int((drop - r0) * SR)
    if rn > 0:
        t = np.arange(rn) / SR
        prog = t / (drop - r0)
        sweep = highpass(rng.standard_normal(rn), 400) * prog ** 2 * 0.35
        tone = np.sin(2 * np.pi * np.cumsum(220 + 880 * prog ** 2) / SR) * prog ** 3 * 0.15
        place(fx, sweep + tone, r0)
    imp_n = int(1.2 * SR)
    impact = lowpass(rng.standard_normal(imp_n), 1800) * env(imp_n, 0.002, 0.4) * 0.6 + \
        np.sin(2 * np.pi * 40 * np.arange(imp_n) / SR) * env(imp_n, 0.002, 0.5) * 0.8
    place(fx, impact, drop)

    mix = drums + (bass + chords) * side + fx
    mix = np.tanh(1.4 * mix / (np.max(np.abs(mix)) + 1e-9)) * 0.9
    # a little stereo width: delay the chords slightly in one channel
    left = mix
    right = mix.copy()
    d = int(0.012 * SR)
    right[d:] = right[d:] * 0.7 + (chords * side)[:-d] * 0.3 / (np.max(np.abs(chords)) + 1e-9) * 0.2
    stereo = np.stack([left, right], axis=1)[: int(duration * SR)]
    pcm = (np.clip(stereo, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return out


if __name__ == "__main__":
    make_track(30, 12, Path("music_test.wav"))
    print("wrote music_test.wav")
