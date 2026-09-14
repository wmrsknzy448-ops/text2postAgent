#!/usr/bin/env python3
"""
HackReel - slide renderer. One 1080x1920 PNG per beat.

Adapted from MLTok Agent's make_card.py, with two changes that its origin as a
one-off local script did not need and this service does:

  * Fonts are resolved, not hardcoded. make_card.py read C:/Windows/Fonts
    directly, which is fatal the moment this runs on a Linux box.
  * Headlines auto-fit. make_card.py used a fixed 150px face; the contract
    allows headlines up to 24 characters, which at that size is roughly 1700px
    wide on a 1080px frame. Text is shrunk and wrapped to fit the safe area.

Why the text is on the image at all: TikTok draft mode transfers media only,
so the caption never reaches the phone. Anything that must be read has to be
drawn here.

Usage:
  python slides.py script.json --outdir ../assets/temp/slides
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_gen  # noqa: E402

W, H = 1080, 1920
BG = (11, 14, 20)
FG = (245, 246, 250)
ACCENT = (255, 78, 66)
MUTED = (138, 148, 168)

# TikTok overlays the bottom ~320px with caption and buttons, and the right
# ~140px with the action rail. Keep everything that matters inside these.
SAFE_TOP, SAFE_BOTTOM, SAFE_X = 260, 360, 90
CONTENT_W = W - 2 * SAFE_X

# Preference order. First existing file wins; the last entries are the ones
# that actually exist in a slim Linux container.
DISPLAY_FACES = ["impact.ttf", "Impact.ttf", "arialbd.ttf", "Arial Bold.ttf",
                 "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"]
BODY_FACES = ["arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf",
              "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf"]

FONT_DIRS = [
    "C:/Windows/Fonts",
    "/usr/share/fonts", "/usr/local/share/fonts",
    "/Library/Fonts", "/System/Library/Fonts",
    os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts"),
]

_font_cache: dict = {}


def _find_face(names: list[str]) -> str | None:
    """Locate the first available face, walking font dirs so Linux packages
    that bury fonts in per-family subdirectories still resolve."""
    key = tuple(names)
    if key in _font_cache:
        return _font_cache[key]
    found = None
    for name in names:
        for d in FONT_DIRS:
            p = Path(d) / name
            if p.is_file():
                found = str(p)
                break
        if found:
            break
    if not found:                       # deep search as a last resort
        wanted = {n.lower() for n in names}
        for d in FONT_DIRS:
            root = Path(d)
            if not root.is_dir():
                continue
            for p in root.rglob("*.ttf"):
                if p.name.lower() in wanted:
                    found = str(p)
                    break
            if found:
                break
    _font_cache[key] = found
    return found


def font(names: list[str], size: int):
    path = _find_face(names)
    if path:
        return ImageFont.truetype(path, size)
    # Never crash on a missing font - a plain slide beats no slide.
    return ImageFont.load_default(size)


def _width(draw, text, f) -> int:
    box = draw.textbbox((0, 0), text, font=f)
    return box[2] - box[0]


def _wrap(draw, text: str, f, max_w: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if _width(draw, trial, f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit(draw, text: str, names: list[str], max_w: int,
        start: int, floor: int, max_lines: int):
    """Largest size at which `text` wraps into at most `max_lines` lines that
    each fit `max_w`. Returns (font, lines)."""
    size = start
    while size > floor:
        f = font(names, size)
        lines = _wrap(draw, text, f, max_w)
        if len(lines) <= max_lines and all(_width(draw, ln, f) <= max_w for ln in lines):
            return f, lines
        size -= 4
    f = font(names, floor)
    return f, _wrap(draw, text, f, max_w)


def _centered(draw, y: int, text: str, f, fill) -> int:
    box = draw.textbbox((0, 0), text, font=f)
    draw.text(((W - (box[2] - box[0])) / 2 - box[0], y - box[1]), text, font=f, fill=fill)
    return box[3] - box[1]


def render_beat(beat: dict, index: int, total: int, out_path: Path,
                include_narration: bool = False) -> Path:
    """Render one slide.

    `include_narration` is the carousel/photo-mode variant. In a narrated
    video the voice carries the explanation and the slide only needs a
    headline; in a carousel nothing is ever heard, so the narration has to be
    drawn or its content is simply lost. The headline gets a smaller starting
    size in that mode to leave room for it.
    """
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    label = (beat.get("label") or beat.get("role", "").replace("_", " ")).upper()
    headline = (beat.get("headline") or "").upper()
    sub = beat.get("sub") or ""
    narration = (beat.get("narration") or "").strip() if include_narration else ""

    # Build the stack, then centre the whole thing in the safe region. Doing
    # it in that order is what keeps both modes balanced without per-mode
    # hand-tuned offsets.
    blocks: list[dict] = []

    if label:
        blocks.append({"lines": [label], "font": font(BODY_FACES, 38),
                       "fill": ACCENT, "pad": 10, "gap": 30, "rule": True})

    f_head, head_lines = fit(
        d, headline, DISPLAY_FACES, CONTENT_W,
        start=130 if include_narration else 190,
        floor=44 if include_narration else 54,
        max_lines=2 if include_narration else 3)
    blocks.append({"lines": head_lines, "font": f_head, "fill": FG,
                   "pad": 12, "gap": 26 if sub else 40})

    if sub:
        f_sub, sub_lines = fit(d, sub, BODY_FACES, CONTENT_W,
                               start=44 if not include_narration else 38,
                               floor=26, max_lines=2)
        blocks.append({"lines": sub_lines, "font": f_sub, "fill": MUTED,
                       "pad": 8, "gap": 46})

    if narration:
        f_narr, narr_lines = fit(d, narration, BODY_FACES, CONTENT_W,
                                 start=48, floor=28, max_lines=7)
        blocks.append({"lines": narr_lines, "font": f_narr, "fill": FG,
                       "pad": 16, "gap": 0})

    def block_h(b: dict) -> int:
        return len(b["lines"]) * (b["font"].size + b["pad"]) + (14 if b.get("rule") else 0)

    total_h = sum(block_h(b) for b in blocks) + sum(b["gap"] for b in blocks[:-1])
    region_top, region_bot = SAFE_TOP, H - SAFE_BOTTOM
    y = region_top + max(0, (region_bot - region_top - total_h) // 2)

    for b in blocks:
        for ln in b["lines"]:
            _centered(d, y, ln, b["font"], b["fill"])
            y += b["font"].size + b["pad"]
        if b.get("rule"):
            d.line([(W / 2 - 80, y), (W / 2 + 80, y)], fill=ACCENT, width=4)
            y += 14
        y += b["gap"]

    # --- progress dots ----------------------------------------------------
    if total > 1:
        r, gap = 7, 26
        span = total * (2 * r) + (total - 1) * (gap - 2 * r)
        dx = (W - span) / 2
        dy = H - SAFE_BOTTOM + 40
        for i in range(total):
            fill = ACCENT if i == index else (58, 66, 82)
            d.ellipse([dx, dy, dx + 2 * r, dy + 2 * r], fill=fill)
            dx += gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def render_script(script: dict, outdir: Path,
                  include_narration: bool = False) -> list[Path]:
    beats = script.get("beats", [])
    outdir.mkdir(parents=True, exist_ok=True)
    return [
        render_beat(b, i, len(beats), outdir / f"slide_{i:02d}.png", include_narration)
        for i, b in enumerate(beats)
    ]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="HackReel slide renderer")
    ap.add_argument("script", help="script.json (bare script or /api/generate result)")
    ap.add_argument("--outdir", default="../assets/temp/slides")
    ap.add_argument("--carousel", action="store_true",
                    help="photo-mode variant: draw the narration onto the slide too")
    a = ap.parse_args()

    sc = script_gen.load_script_file(a.script)
    disp, body = _find_face(DISPLAY_FACES), _find_face(BODY_FACES)
    print(f"display font : {disp or 'PIL default (no TTF found!)'}")
    print(f"body font    : {body or 'PIL default (no TTF found!)'}")
    for p in render_script(sc, Path(a.outdir), a.carousel):
        print(f"wrote {p}  ({p.stat().st_size} bytes)")
