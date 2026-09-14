#!/usr/bin/env python3
"""
HackReel - assembly. Turns a script into a finished, publishable bundle.

Carousel path (implemented): renders the photo-mode slides, validates them
against TikTok's photo-post constraints, builds a contact sheet for review,
and writes post.json describing exactly what publish.py should send.

Video path (todo): needs ffmpeg to cut slides against the per-beat durations
in voice.py's manifest.

Two decisions are deliberate and worth not undoing:

  * The carousel renders its own slides with `include_narration=True`. Reusing
    the video slides would silently drop the explanation, because in photo
    mode nothing is ever heard.
  * post.json carries `privacy: SELF_ONLY` and `draft: true` as data, not as a
    default buried in publish.py. The artifact declares its own intended
    blast radius, so publish.py can assert on it instead of assuming.

Usage:
  python assemble.py script.json --format carousel
  python assemble.py script.json --format carousel --outdir ../assets/output
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_gen  # noqa: E402
import slides as slides_mod  # noqa: E402
import voice as voice_mod  # noqa: E402

# TikTok photo posts: up to 35 images. Keep a floor of 1 - MLTok Agent
# confirmed a single image ships as a 1-item photo carousel.
MIN_IMAGES, MAX_IMAGES = 1, 35
MAX_IMAGE_BYTES = 20 * 1024 * 1024
EXPECT_W, EXPECT_H = slides_mod.W, slides_mod.H


class AssemblyError(Exception):
    """The bundle is not fit to publish."""


def _contact_sheet(paths: list[Path], out: Path, cols: int = 5, thumb_w: int = 260) -> Path:
    """One image showing every slide in order, for review before publishing.

    Worth the few lines: this is what makes a phone-approval message actually
    reviewable. Approving a post you cannot see is not approval.
    """
    thumb_h = round(thumb_w * EXPECT_H / EXPECT_W)
    cols = min(cols, len(paths)) or 1
    rows = (len(paths) + cols - 1) // cols
    pad = 12
    sheet = Image.new(
        "RGB",
        (cols * thumb_w + pad * (cols + 1), rows * thumb_h + pad * (rows + 1)),
        (8, 10, 15),
    )
    for i, p in enumerate(paths):
        im = Image.open(p).resize((thumb_w, thumb_h), Image.LANCZOS)
        x = pad + (i % cols) * (thumb_w + pad)
        y = pad + (i // cols) * (thumb_h + pad)
        sheet.paste(im, (x, y))
    sheet.save(out, "PNG", optimize=True)
    return out


def validate_images(paths: list[Path]) -> list[dict]:
    if not (MIN_IMAGES <= len(paths) <= MAX_IMAGES):
        raise AssemblyError(f"{len(paths)} images, must be {MIN_IMAGES}-{MAX_IMAGES}")

    info = []
    for i, p in enumerate(paths):
        if not p.is_file():
            raise AssemblyError(f"missing image: {p}")
        size = p.stat().st_size
        if size == 0:
            raise AssemblyError(f"empty image: {p}")
        if size > MAX_IMAGE_BYTES:
            raise AssemblyError(f"{p.name} is {size} bytes, over the {MAX_IMAGE_BYTES} limit")
        with Image.open(p) as im:
            w, h = im.size
        if (w, h) != (EXPECT_W, EXPECT_H):
            raise AssemblyError(f"{p.name} is {w}x{h}, expected {EXPECT_W}x{EXPECT_H}")
        info.append({"index": i, "path": str(p), "bytes": size, "width": w, "height": h})
    return info


def assemble_carousel(script: dict, outdir: Path) -> dict:
    """Render, validate, and describe a photo-mode post."""
    beats = script.get("beats") or []
    if not beats:
        raise AssemblyError("script has no beats")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = outdir / f"{script_gen.slugify(script.get('project', 'hackreel'))}-{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)

    paths = slides_mod.render_script(script, bundle, include_narration=True)
    images = validate_images(paths)
    for img, beat in zip(images, beats):
        img["role"] = beat.get("role", "")
        img["headline"] = beat.get("headline", "")

    sheet = _contact_sheet(paths, bundle / "contact_sheet.png")

    post = {
        "format": "carousel",
        "media_type": "photo",          # TikTok rejects text-only posts
        "project": script.get("project", ""),
        "cover_index": 0,               # first slide is the thumbnail
        "images": images,
        "contact_sheet": str(sheet),
        # Recorded for a real publish. In draft mode the caption does NOT
        # reach the TikTok app - that is why the text is on the slides.
        "caption": script.get("caption", ""),
        "hashtags": script.get("hashtags", []),
        "caption_reaches_app": False,
        # Safe defaults travel with the artifact, not as publish.py's opinion.
        "privacy": "SELF_ONLY",
        "draft": True,
        "approved_for_real_publish": False,
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema": "hackreel/post@1",
    }
    (bundle / "post.json").write_text(
        json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")

    # Persist the full script beside the post. post.json deliberately carries
    # only what publishing needs (paths, headlines), which meant narration and
    # sub existed nowhere on disk once the browser tab closed - so a bundle
    # could not be re-rendered, audited, or style-checked after the fact.
    (bundle / "script.json").write_text(
        json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    return post


FFMPEG = os.environ.get("HACKREEL_FFMPEG", "ffmpeg")

# A slide that cuts on the last syllable feels clipped. Holding each one a
# beat past its narration is the difference between a slideshow and something
# watchable. Applied per slide, so it also nudges the pacing apart.
SLIDE_TAIL_PAD = 0.35


def ffmpeg_version() -> str | None:
    """The binary's version line, or None when it is not on PATH."""
    if not shutil.which(FFMPEG):
        return None
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True,
                             timeout=20)
        return (out.stdout or "").splitlines()[0] if out.stdout else "unknown"
    except (OSError, subprocess.SubprocessError):
        return None


def probe_duration(path: Path) -> float | None:
    """True decoded duration of a media file, via ffprobe.

    voice.py derives its durations from edge-tts WordBoundary events, which
    measure SPEECH, not the encoded file. mp3 frame padding makes each clip
    land ~0.37s longer than the boundaries claim - small per clip, but it
    accumulates, and by the final beat the slides would be well over a second
    ahead of the voice. Slide timing must come from the file, not the estimate.
    """
    probe = shutil.which("ffprobe") or shutil.which(FFMPEG.replace("ffmpeg", "ffprobe"))
    if not probe:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return float((out.stdout or "").strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _concat_file(path: Path, entries: list[str]) -> Path:
    """Write an ffmpeg concat-demuxer list.

    Paths are written POSIX-style even on Windows: the demuxer treats a
    backslash as an escape, so C:\\x\\y silently becomes C:xy and ffmpeg
    reports a missing file that is plainly there.
    """
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return path


def assemble_video(script: dict, outdir: Path,
                   voice: str | None = None, rate: str | None = None) -> dict:
    """Slides + narration -> one narrated vertical mp4.

    Each slide is held for its own beat's MEASURED narration length, taken
    from voice.py's manifest rather than any words-per-second estimate. That
    matters: the estimate was 28% out before it was recalibrated, and a slide
    that changes half a sentence early is immediately obvious.
    """
    version = ffmpeg_version()
    if not version:
        raise AssemblyError(
            f"ffmpeg not found (looked for {FFMPEG!r} on PATH). The carousel path "
            "works without it; the video path cannot. Install it, or set "
            "HACKREEL_FFMPEG to the binary's full path."
        )

    beats = script.get("beats") or []
    if not beats:
        raise AssemblyError("script has no beats")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = outdir / f"{script_gen.slugify(script.get('project', 'hackreel'))}-{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)

    # Video-mode slides: no narration drawn on, because it is spoken.
    frames = slides_mod.render_script(script, bundle, include_narration=False)
    validate_images(frames)

    manifest = voice_mod.render_script(
        script, bundle / "audio",
        voice or voice_mod.DEFAULT_VOICE, rate or voice_mod.DEFAULT_RATE)
    tracks = manifest["beats"]

    if len(tracks) != len(frames):
        raise AssemblyError(
            f"{len(frames)} slides but {len(tracks)} narration clips - refusing to "
            "guess which slide belongs to which line")

    # Prefer the real encoded length of each clip over the boundary estimate.
    for t in tracks:
        actual = probe_duration(Path(t["mp3"]))
        t["measured_sec"] = round(actual, 3) if actual else t["duration_sec"]
        t["estimate_sec"] = t["duration_sec"]

    # Image list. The concat demuxer applies each `duration` to the file BEFORE
    # it, and ignores the last file's duration entirely - so the final frame is
    # repeated. That repeat inherits the previous duration unless given its
    # own, which silently added a phantom trailing segment; pin it to one frame.
    img_entries: list[str] = []
    for frame, track in zip(frames, tracks):
        img_entries.append(f"file '{frame.resolve().as_posix()}'")
        img_entries.append(f"duration {track['measured_sec'] + SLIDE_TAIL_PAD:.3f}")
    img_entries.append(f"file '{frames[-1].resolve().as_posix()}'")
    img_entries.append("duration 0.033")
    img_list = _concat_file(bundle / "frames.txt", img_entries)

    aud_list = _concat_file(
        bundle / "audio.txt",
        [f"file '{Path(t['mp3']).resolve().as_posix()}'" for t in tracks])

    out_mp4 = bundle / "video.mp4"
    TOTAL = sum(t["measured_sec"] for t in tracks) + SLIDE_TAIL_PAD * len(tracks)
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0", "-i", str(img_list),
        "-f", "concat", "-safe", "0", "-i", str(aud_list),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",          # without this many players show nothing
        "-r", "30", "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        # No -shortest: the video runs slightly past the audio by design, so
        # the closing slide holds instead of cutting on the final word.
        # -t pins the total: the concat demuxer's trailing-frame handling is
        # fiddly enough that the output length is worth asserting outright.
        "-t", f"{TOTAL:.3f}",
        str(out_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not out_mp4.is_file() or out_mp4.stat().st_size == 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-12:])
        raise AssemblyError(f"ffmpeg failed (exit {proc.returncode}):\n{tail}")

    expected = TOTAL
    post = {
        "format": "video",
        "media_type": "video",
        "project": script.get("project", ""),
        "video": str(out_mp4),
        "video_bytes": out_mp4.stat().st_size,
        "duration_sec": round(expected, 2),
        "width": slides_mod.W, "height": slides_mod.H,
        "beats": [{"index": t["index"], "role": t["role"],
                   "headline": b.get("headline", ""),
                   "duration_sec": t["measured_sec"],
                   "estimate_sec": t["estimate_sec"]}
                  for t, b in zip(tracks, beats)],
        "contact_sheet": str(_contact_sheet(frames, bundle / "contact_sheet.png")),
        "caption": script.get("caption", ""),
        "hashtags": script.get("hashtags", []),
        "caption_reaches_app": False,
        "privacy": "SELF_ONLY",
        "draft": True,
        "approved_for_real_publish": False,
        "voice": manifest["voice"],
        "ffmpeg": version,
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "schema": "hackreel/post@1",
    }
    (bundle / "post.json").write_text(
        json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
    (bundle / "script.json").write_text(
        json.dumps(script, indent=2, ensure_ascii=False), encoding="utf-8")
    return post


def assemble(script: dict, outdir: Path, fmt: str = "auto") -> dict:
    if fmt == "auto":
        fmt = "carousel" if script_gen.est_duration(script) < script_gen.CAROUSEL_MAX_SEC \
            else "video"
    if fmt == "carousel":
        return assemble_carousel(script, outdir)
    if fmt == "video":
        return assemble_video(script, outdir)
    raise AssemblyError(f"unknown format: {fmt!r}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="HackReel assembly")
    ap.add_argument("script")
    ap.add_argument("--outdir", default="../assets/output")
    ap.add_argument("--format", default="carousel", choices=["auto", "carousel", "video"])
    a = ap.parse_args()

    sc = script_gen.load_script_file(a.script)
    try:
        post = assemble(sc, Path(a.outdir), a.format)
    except AssemblyError as e:
        sys.stderr.write(f"assembly failed: {e}\n")
        raise SystemExit(1)

    print(f"format  : {post['format']} ({post['media_type']})")
    print(f"privacy : {post['privacy']}  draft={post['draft']}")

    if post["format"] == "video":
        for b in post["beats"]:
            print(f"  [{b['index']}] {b['role']:<22} {b['duration_sec']:>6.2f}s  {b['headline']}")
        mb = post["video_bytes"] / 1024 / 1024
        print(f"video   : {Path(post['video']).name}  {post['width']}x{post['height']}  "
              f"{post['duration_sec']}s  {mb:.1f} MB")
        print(f"voice   : {post['voice']}")
        bundle_dir = Path(post["video"]).parent
    else:
        for im in post["images"]:
            print(f"  [{im['index']}] {Path(im['path']).name}  {im['width']}x{im['height']}  "
                  f"{im['bytes']:>7} B  {im['headline']}")
        bundle_dir = Path(post["images"][0]["path"]).parent

    print(f"sheet   : {post['contact_sheet']}")
    print(f"bundle  : {bundle_dir}")
