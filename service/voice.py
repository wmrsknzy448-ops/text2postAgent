#!/usr/bin/env python3
"""
HackReel - narration. One mp3 per beat, plus a manifest of real durations.

Adapted from MLTok Agent's make_voice.py. The change that matters: MLTok
rendered the whole script as a single mp3, which is all a talking-head clip
needs. Here each slide has to stay on screen exactly as long as its own line
takes to say, so audio is rendered per beat and the measured duration of each
is written to a manifest for assemble.py to cut against.

Durations come from edge-tts WordBoundary events (100-nanosecond units), so no
ffprobe or mutagen is needed to measure the files. Word-level boundaries are
opt-in - edge-tts 7.x defaults to SentenceBoundary - and they are also what
lets captions land on the exact frame a word is spoken.

Note: edge-tts sends the narration text to Microsoft's TTS endpoint. It needs
no API key, but the text does leave the machine.

Usage:
  python voice.py script.json --outdir ../assets/temp/voice
  python voice.py script.json --voice en-US-AndrewNeural --rate +8%
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import edge_tts

sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_gen  # noqa: E402

DEFAULT_VOICE = "en-US-AndrewNeural"
DEFAULT_RATE = "+0%"

# edge-tts reports offsets in 100ns ticks.
TICKS_PER_SEC = 10_000_000


async def _render(text: str, voice: str, rate: str, base: Path) -> tuple[Path, Path, float]:
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    sub = edge_tts.SubMaker()
    mp3, srt = base.with_suffix(".mp3"), base.with_suffix(".srt")
    base.parent.mkdir(parents=True, exist_ok=True)

    last_tick = 0
    with open(mp3, "wb") as fh:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif str(chunk["type"]).endswith("Boundary"):
                sub.feed(chunk)
                last_tick = max(last_tick, chunk["offset"] + chunk["duration"])

    srt.write_text(sub.get_srt(), encoding="utf-8")

    if last_tick:
        duration = last_tick / TICKS_PER_SEC
    else:
        # No boundary events came back; fall back to the same estimate the
        # script contract uses rather than reporting a bogus zero.
        duration = len(text.split()) / script_gen.WORDS_PER_SEC
    return mp3, srt, round(duration, 3)


def render_script(script: dict, outdir: Path,
                  voice: str = DEFAULT_VOICE, rate: str = DEFAULT_RATE) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    beats = script.get("beats", [])
    entries = []

    for i, b in enumerate(beats):
        text = (b.get("narration") or "").strip()
        if not text:
            raise ValueError(f"beat {i} ({b.get('role')}) has no narration")
        mp3, srt, dur = asyncio.run(_render(text, voice, rate, outdir / f"beat_{i:02d}"))
        entries.append({
            "index": i,
            "role": b.get("role", ""),
            "mp3": str(mp3),
            "srt": str(srt),
            "duration_sec": dur,
            "words": len(text.split()),
            "narration": text,
        })

    manifest = {
        "project": script.get("project", ""),
        "voice": voice,
        "rate": rate,
        "beats": entries,
        "total_duration_sec": round(sum(e["duration_sec"] for e in entries), 3),
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="HackReel narration renderer")
    ap.add_argument("script")
    ap.add_argument("--outdir", default="../assets/temp/voice")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--rate", default=DEFAULT_RATE)
    a = ap.parse_args()

    sc = script_gen.load_script_file(a.script)
    m = render_script(sc, Path(a.outdir), a.voice, a.rate)

    print(f"voice : {m['voice']}  rate {m['rate']}")
    est = script_gen.est_duration(sc)
    for e in m["beats"]:
        print(f"  [{e['index']}] {e['role']:<22} {e['duration_sec']:>6.2f}s  "
              f"{Path(e['mp3']).name}")
    print(f"total : {m['total_duration_sec']}s measured  (contract estimated {est}s)")
