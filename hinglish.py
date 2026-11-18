"""Turn Devanagari Hindi into Hinglish (Roman script, the way people type it: "kya scene hai bhai").

Claude does it when an API key is available (most natural spellings); otherwise a rule-based
transliterator with Hindi schwa deletion ("करना" -> "karna", not "karanā")."""
from __future__ import annotations

import os
import re
from dataclasses import replace

from pydantic import BaseModel

from sources import Word

DEVANAGARI = re.compile(r"[ऀ-ॿ]")

CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
    "ञ": "n", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d",
    "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
    "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "rh", "फ़": "f", "य़": "y",
}
NUKTA_FORMS = {"क़": "क़", "ख़": "ख़", "ग़": "ग़", "ज़": "ज़", "ड़": "ड़",
               "ढ़": "ढ़", "फ़": "फ़", "य़": "य़"}
VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e",
          "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o"}
MATRAS = {"ा": "a", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai",
          "ो": "o", "ौ": "au", "ॉ": "o"}
VIRAMA, NUKTA = "्", "़"
NASAL = {"ं": "\x01", "ँ": "\x01", "ः": "h"}  # \x01 = nasal, resolved below


def _roman_word(w: str) -> str:
    for k, v in NUKTA_FORMS.items():
        w = w.replace(k, v)
    # syllables: [consonant, vowel] where vowel None = inherent schwa, "" = virama
    syl: list[list] = []
    i = 0
    while i < len(w):
        ch = w[i]
        if ch in CONS or (ch + NUKTA) in CONS and i + 1 < len(w) and w[i + 1] == NUKTA:
            c = ch
            if i + 1 < len(w) and w[i + 1] == NUKTA:
                c, i = ch + NUKTA, i + 1
            syl.append([CONS.get(c, CONS.get(ch, "")), None])
        elif ch in MATRAS and syl:
            syl[-1][1] = MATRAS[ch]
        elif ch == VIRAMA and syl:
            syl[-1][1] = ""
        elif ch in VOWELS:
            syl.append(["", VOWELS[ch]])
        elif ch in NASAL and syl:
            syl[-1].append(NASAL[ch])
        else:
            syl.append([ch, ""])
        i += 1
    # schwa deletion: drop the word-final schwa, and a medial schwa between two voweled syllables
    if len(syl) > 1 and syl[-1][1] is None and syl[-1][0]:
        syl[-1][1] = ""
    for j in range(len(syl) - 2, 0, -1):
        if (syl[j][1] is None and syl[j][0] and syl[j - 1][1] not in ("", None) or
                (syl[j][1] is None and j - 1 == 0 and syl[0][1] is None)):
            if syl[j + 1][1] not in ("",) and syl[j + 1][0]:
                syl[j][1] = ""
                break
    # closed final syllable keeps a long "aa": यार -> yaar, काम -> kaam, बात -> baat
    if len(syl) >= 2 and syl[-1][1] == "" and syl[-2][1] == "a" and syl[-2][0]:
        syl[-2][1] = "aa"
    out = ""
    for s in syl:
        v = "a" if s[1] is None else s[1]
        out += s[0] + v + (s[2] if len(s) > 2 else "")
    # casual spelling: long vowels read as short at word end ("bhaee" -> "bhai")
    out = re.sub(r"ee\x01?$", "i", out)   # भाई -> bhai, नहीं -> nahi
    out = re.sub(r"oo\x01$", "un", out)   # हूँ -> hun
    out = re.sub(r"oo$", "u", out)
    return out.replace("\x01", "n")


def rule_based(text: str) -> str:
    return re.sub(r"[ऀ-ॿ]+", lambda m: _roman_word(m.group()), text)


class _Map(BaseModel):
    roman: list[str]


_claude_ok = True
MODEL: str | None = "claude-opus-5-5"   # set by the CLI from --judge; None = built-in transliterator only


def _claude(tokens: list[str], model: str) -> list[str] | None:
    global _claude_ok
    if not _claude_ok or not model or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        resp = anthropic.Anthropic().messages.parse(
            model=model, max_tokens=8000, output_config={"effort": "low"}, output_format=_Map,
            messages=[{"role": "user", "content":
                "Transliterate each Hindi word into casual Hinglish (Roman script, how Indian Gen-Z "
                "types on YouTube/Instagram: 'kya', 'bhai', 'nahi', 'scene', 'mast'). English loanwords "
                "go back to normal English spelling. Return exactly one output per input, same order.\n"
                + "\n".join(tokens)}],
        )
        out = resp.parsed_output.roman if resp.parsed_output else None
        return out if out and len(out) == len(tokens) else None
    except Exception as e:
        print(f"    (Hinglish via Claude unavailable: {type(e).__name__}; using built-in transliterator)")
        _claude_ok = False
        return None


def to_hinglish(words: list[Word], model: str | None = None) -> list[Word]:
    model = model or MODEL
    uniq = sorted({w.text for w in words if DEVANAGARI.search(w.text)})
    if not uniq:
        return words
    mapped = _claude(uniq, model)
    table = dict(zip(uniq, mapped)) if mapped else {u: rule_based(u) for u in uniq}
    return [replace(w, text=table.get(w.text, w.text)) for w in words]


class _Tr(BaseModel):
    english: list[str]


def translate_phrases(phrases: list[str], model: str | None = None) -> list[str] | None:
    """Translate caption phrases (Hindi/Hinglish/any) into short natural English subtitles, one per input."""
    model = model or MODEL
    if not phrases or not model or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        prompt = ("These are consecutive subtitle lines from a livestream (often Hindi/Hinglish, transcribed by "
                  "speech recognition, so expect errors). Translate each line into short, natural, casual English "
                  "subtitles that keep the streamer's energy. Use the surrounding lines for context. Return exactly "
                  "one English line per input line, same order; if a line is unintelligible, return a best guess "
                  "or an empty string.\n\n" + "\n".join(f"{i + 1}. {p}" for i, p in enumerate(phrases)))
        resp = anthropic.Anthropic().messages.parse(
            model=model, max_tokens=8000, output_config={"effort": "low"}, output_format=_Tr,
            messages=[{"role": "user", "content": prompt}])
        out = resp.parsed_output.english if resp.parsed_output else None
        return out if out and len(out) == len(phrases) else None
    except Exception as e:
        print(f"    (English captions unavailable: {type(e).__name__}; falling back to Hinglish)")
        return None
