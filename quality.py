"""Catch clips that aren't worth posting because of the footage itself: mostly black (stream offline, scene
switch), or frozen (loading screens, pause menus, a stuck feed). Checked on the source before rendering."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


class Junk(Exception):
    """The footage is mostly black or frozen; the clip is skipped."""


def junk_share(src: Path, start: float, length: float) -> tuple[float, float]:
    """(share of the stretch that's black, share that's frozen), analysed at low resolution."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.2f}", "-t", f"{length:.2f}",
                        "-i", str(src), "-an", "-vf",
                        "scale=320:-2,fps=10,blackdetect=d=0.5:pix_th=0.08,freezedetect=n=0.003:d=1.5",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    black = sum(float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", r.stderr))
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", r.stderr))
    # a freeze still running at the end has no duration line: count it up to the end
    starts = [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", r.stderr)]
    ends = re.findall(r"freeze_end:", r.stderr)
    if len(starts) > len(ends):
        frozen += max(0.0, length - starts[-1])
    return min(1.0, black / length), min(1.0, frozen / length)


def check(src: Path, start: float, length: float, limit: float = 0.5) -> None:
    """Raise Junk if more than `limit` of the clip is black or frozen."""
    if length <= 0:
        return
    black, frozen = junk_share(src, start, length)
    if black > limit:
        raise Junk(f"{black:.0%} of the footage is black")
    if frozen > limit:
        raise Junk(f"{frozen:.0%} of the footage is frozen (loading screen, menu or a stuck feed)")
