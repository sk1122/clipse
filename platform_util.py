"""The few things that differ between macOS, Linux and Windows: fonts and opening files/links."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

# (path, face index) candidates per role, best first. The first one that exists is used; if none do,
# fontconfig (`fc-match`, standard on Linux) is asked for the family names.
FONTS = {
    "caption": {   # heavy, punchy captions
        "files": [("/System/Library/Fonts/Supplemental/Arial Black.ttf", 0),
                  ("/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf", 0),
                  ("/usr/share/fonts/truetype/noto/NotoSans-Black.ttf", 0),
                  ("/usr/share/fonts/noto/NotoSans-Black.ttf", 0),
                  ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
                  ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", 0),
                  ("C:/Windows/Fonts/ariblk.ttf", 0)],
        "families": ["Arial Black", "Noto Sans:black", "Noto Sans:bold", "DejaVu Sans:bold", "sans:bold"],
    },
    "hook": {      # bold, for the banner at the top
        "files": [("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
                  ("/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf", 0),
                  ("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf", 0),
                  ("/usr/share/fonts/noto/NotoSans-Bold.ttf", 0),
                  ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
                  ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", 0),
                  ("C:/Windows/Fonts/arialbd.ttf", 0)],
        "families": ["Arial:bold", "Noto Sans:bold", "DejaVu Sans:bold", "sans:bold"],
    },
    "devanagari": {  # Hindi captions
        "files": [("/System/Library/Fonts/Kohinoor.ttc", 3),
                  ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 0),
                  ("/usr/share/fonts/noto/NotoSansDevanagari-Bold.ttf", 0),
                  ("/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf", 0),
                  ("/usr/share/fonts/truetype/fonts-deva-extra/kalimati.ttf", 0),
                  ("C:/Windows/Fonts/NirmalaB.ttf", 0)],
        "families": ["Noto Sans Devanagari:bold", "Lohit Devanagari", "sans:lang=hi"],
    },
}


@lru_cache(maxsize=None)
def font(role: str) -> tuple[str, int] | None:
    """Best available font file for a role, as (path, face index); None means use Pillow's default."""
    spec = FONTS[role]
    for path, idx in spec["files"]:
        if os.path.exists(path):
            return path, idx
    if shutil.which("fc-match"):
        for fam in spec["families"]:
            try:
                out = subprocess.run(["fc-match", "-f", "%{file}\t%{index}", fam], capture_output=True,
                                     text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            path, _, idx = out.partition("\t")
            if path and os.path.exists(path):
                return path, int(idx or 0)
    return None


def open_path(target: str | Path, reveal: bool = False) -> None:
    """Open a file, folder or URL with the system's default app. reveal=True shows a file in its folder."""
    target = str(target)
    if sys.platform == "darwin":
        cmd = ["open", "-R", target] if reveal else ["open", target]
    elif sys.platform.startswith("win"):
        if reveal:
            subprocess.Popen(["explorer", "/select,", target])
        else:
            os.startfile(target)  # noqa: S606 — opening the user's own file/URL
        return
    else:
        if reveal and os.path.isfile(target):
            target = os.path.dirname(target)
        cmd = ["xdg-open", target]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
