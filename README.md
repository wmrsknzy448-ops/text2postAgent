# HackReel

Prompt-to-post video generator for hackathon project pitches. Describe what you
built; get a narrated vertical video or an image carousel, ready to push to
TikTok as a draft.

**This is a standalone service.** It runs on a server with nothing but a
`GEMINI_API_KEY` in its environment. Claude Code was used to build it and is
not involved at runtime — no skills, no terminal, no operator in the loop.

## Run it

**Requirements:** Python 3.10 or newer. A Gemini API key — free, no credit card,
from <https://aistudio.google.com/apikey>. ffmpeg and ffprobe on `PATH` are
needed only for the video path; the image-carousel path works without them.

### Windows (PowerShell)

```powershell
cd hackreel
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:GEMINI_API_KEY = "your-key-here"
.\.venv\Scripts\python.exe -m uvicorn service.main:app --port 8000
```

### macOS / Linux

```bash
cd hackreel
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"
.venv/bin/python -m uvicorn service.main:app --port 8000
```

Then open <http://127.0.0.1:8000/>.

`GEMINI_API_KEY` is the only variable needed to run the service. The key is read
from the environment and is never written to a file. The Zernio and ntfy
settings further down are for command-line publishing only — `main.py` never
imports `publish.py`, so the web UI runs without them.

Three notes, each of which cost someone an hour:

- **Install from `requirements.txt`, not a package list.** Pillow and edge-tts
  are easy to miss, and Pillow fails at *import* time — the server will not
  start at all without it.
- **Call the venv's interpreter directly** rather than activating. On Windows the
  default execution policy is `Restricted`, where `Activate.ps1` refuses to run;
  `.\.venv\Scripts\python.exe -m uvicorn` sidesteps activation entirely.
- **Linux/macOS: install fonts.** Slide rendering looks for DejaVu, Liberation
  or Noto. A slim container has none and silently falls back to a generic face —
  it still renders, but the headlines lose their impact. On Debian/Ubuntu:
  `sudo apt-get install -y fonts-dejavu-core fonts-liberation`

Verified on Windows with Python 3.12.10 from a clean clone and a fresh venv: the
server starts, `/api/health` responds, and both carousel and video render. The
macOS/Linux commands follow the standard venv layout but were not run on those
platforms; Python 3.10 and 3.11 are the dependency floor, tested only on 3.12.

## Layout

```
service/
  script_gen.py   Gemini API. Rubric + story shapes + two-pass self-critique.
  main.py         FastAPI backend.
  voice.py        narration via edge-tts, one mp3 per beat + measured durations
  slides.py       one 1080x1920 slide per beat
  assemble.py     carousel bundle, or narrated mp4 via ffmpeg; + post.json
  publish.py      Zernio -> TikTok draft, behind the ntfy approval gate
web/index.html    Single-page UI.
assets/output/    Finished files.
assets/temp/      Intermediates.
```

`.mcp.json` and `.claude/settings.json` are **dev-time only** — they configure
Claude Code for working on this repo and are not read by the running service.

## How the script generator avoids slop

A weak prompt is the enemy. The defence is four layers, because a single prompt
instruction does not hold:

1. **Story shapes, not a template.** The model picks one of five shapes
   (`problem_first`, `almost_broke`, `number_first`, `wrong_assumption`,
   `demo_cold_open`) based on what the input actually supports. The shape
   decides beat order and weight, so output is structurally varied rather than
   a fixed five-slot form. Each shape carries an *avoid when* as well as a
   *use when*, and the model records which detail drove its choice.
2. **A rubric it self-checks against.** R1 concrete anchor, R2 earned hook,
   R3 honest scope, R4 earned middle, R5 spoken rhythm. Pass/fail, each needing
   a verbatim quote as evidence.
3. **An adversarial second pass.** A separate call critiques the draft against
   the same rubric and either revises it, leaves it alone, or gives up and asks
   a question. It is explicitly forbidden from repairing a *grounding* failure
   by supplying the missing fact itself.
4. **Deterministic guards that outrank both passes.** Regex and arithmetic, not
   judgement: beat counts, word and character limits, role structure, banned
   openers, marketing register, and — most importantly — **every digit in the
   output must trace to a number in the input**, with spelled-out numbers
   normalised. The critic does not get the last word; if the guards fail after
   revision, the result is downgraded to a question.

**A thin prompt returns a specific follow-up question, never padded filler.**
This is the point. Inventing a plausible number is worse than returning nothing,
because nothing downstream catches it.

## Safety rules (carried from MLTok Agent, non-negotiable)

- Nothing is published or sent without explicit phone approval first, via the
  ntfy gate. Fail closed on timeout or any error.
- `SELF_ONLY` + draft for anything not explicitly approved as a real publish.
- No API keys hardcoded in files, committed, or pasted into chat.

`/api/publish` returns 501 by design. `publish.py` exists and is tested, but
exposing it over HTTP raises a question that has to be answered first — see
below. Run it from the command line meanwhile:

```bash
python service/publish.py assets/output/<bundle>          # dry run, sends nothing
python service/publish.py assets/output/<bundle> --send   # upload + phone gate + draft
python service/publish.py assets/output/<bundle> --test-gate   # ntfy round trip only
python service/test_publish.py                            # 60 offline safety tests
```

Dry run is the default. `--send` is the only way to touch the network.

### Open question: a public UI plus a publish endpoint

The product goal is that a judge or stranger can drive this from a UI. But if
`/api/publish` is reachable by them, a stranger can make the owner's phone ring
with an approval prompt. Unlimited prompts from people whose content the owner
did not write is a straight path to rubber-stamping — **alert fatigue is the
documented failure mode of this exact gate**, and a gate that cries wolf is
worse than no gate.

Generate and render are safe to expose. Publishing probably needs to stay
operator-only (CLI, or an authenticated route), rather than being a button
anyone can press.

### Environment

`publish.py` reads all of these from the environment; none are in the repo:

| Var | Purpose |
|---|---|
| `GEMINI_API_KEY` | script generation (free tier, no card) |
| `HACKREEL_MODEL` | optional; default `gemini-3.6-flash`, with automatic failover through `FALLBACK_MODELS`. Note `gemini-2.5-pro` hits quota and `gemini-2.5-flash` 404s even though `models.list()` advertises it. |
| `ZERNIO_API_KEY` | Zernio auth |
| `ZERNIO_ACCOUNT_ID` | the TikTok account to post to |
| `NTFY_TOPIC`, `NTFY_REPLY_TOPIC`, `NTFY_SERVER` | approval channel (falls back to `~/.claude/mltok-ntfy.conf`) |

## Status

| Piece | State |
|---|---|
| `script_gen.py` prompts + guards | done, guards unit-tested |
| `script_gen.py` live API call | **verified live** 2026-08-26 on `gemini-3.5-flash`: full two-pass run in 34.2s, all five rubric criteria passed, critic returned `unchanged` |
| thin-prompt / follow-up-question path | **verified live 2026-09-07** — the writer invented a claim ("published our unfinished hackathon code"), the critic caught it and returned a question instead of a script. Note it also over-flagged one grounded phrase; any single ungrounded flag blocks the whole script. |
| `main.py` `/api/health`, `/api/generate` | done, tested |
| `main.py` `/api/render` | live for carousel and video (health reports which formats ffmpeg allows) |
| `main.py` `/api/publish` | 501, fails closed by design |
| `web/index.html` | done, tested (generate / question / retry / error) |
| `voice.py` | done, produces real audio; durations measured, not guessed |
| `slides.py` | done, renders real PNGs; auto-fit verified against overflow |
| `assemble.py` carousel | done, end to end through the UI |
| `assemble.py` video | done — 1080x1920 h264/aac, slides cut to **ffprobe-measured** clip lengths; verified 0.003s drift and frame-accurate sync |
| `publish.py` — ntfy approval gate | 60 offline tests pass; **verified live** (approve path and deny-on-timeout, against real ntfy traffic) |
| `publish.py` — Zernio/TikTok path | **VERIFIED LIVE 2026-08-31.** Carousel published to the Creator Inbox: `platformPostId: p_inbox_url~v2.example`, `isDraft: true`, `privacy_level: SELF_ONLY` echoed back. Receipt at `assets/output/hackreel-20260831-173342/publish_receipt.json`. |
| `publish.py` — video post | **VERIFIED LIVE 2026-09-07.** `media_type: "video"`, one `video`-typed mediaItem: `v_inbox_url~v2.example`, `isDraft: true`, `SELF_ONLY` echoed back. |
| `test_publish.py` | 60 assertions, fully offline |
| ffmpeg | installed (9.0, winget/Gyan) and on PATH |

### Retries need a timeout, or they make things worse

Measured 2026-08-26: an overloaded `gemini-3.7-flash` did **not** fail fast on
503 — it hung for **263.8s** before answering. Retrying that without a per-call
timeout would have turned one 503 into a ~17-minute wait on the writer pass
alone. `REQUEST_TIMEOUT_MS = 90_000` is therefore load-bearing, not a nicety:
**never add retries here without it.**

Retries wrap each API *call*, not the whole generation. Retrying the endpoint
would re-run the writer pass when only the critic failed — burning a second
request of scarce free-tier quota and judging a different draft than the one
the critic saw. Only errors `describe_error()` marks retryable are retried; a
bad key, a bad schema or a safety block fails immediately.

Worst case is still ~4.5 minutes with the browser holding the connection. A job
id plus polling is the real fix if that becomes a problem.

### Newest model is not the best default

Measured on a trivial structured call: `gemini-3.5-flash` 1.7s, `3.6-flash`
2.4s, `3-flash-preview` 3.3s, `3.1-flash-lite` 5.3s, `3.7-flash` **15.7s** —
and 3.7 was the one returning "high demand" 503s. Default is `gemini-3.5-flash`.

Also: `models.list()` advertising a model is **not** proof you can call it.
`gemini-2.5-flash` is listed on this free-tier key and 404s when called.
Avoid the `*-latest` aliases — they float, and a graded demo wants a model that
does not change underneath it.

### publishNow vs draft: answered by a live run

The docs still state no precedence (checked /platforms/tiktok,
/posts/create-post, /guides/platform-settings on 2026-08-28). A real send on
2026-08-31 settled it empirically: with `publishNow: true` AND
`tiktokSettings.draft: true`, **draft wins** - the post landed in the Creator
Inbox (`platformPostId` prefixed `p_inbox_url~`) rather than the feed, with
`privacy_level: SELF_ONLY` and `draft: true` echoed back unchanged.

Keep `SELF_ONLY` anyway. It costs nothing and it is the only thing standing
between a config slip and a public post.

### The inbox-URL prefix changes with media type

`platformPostId` comes back `p_inbox_url~…` for a photo carousel and
`v_inbox_url~…` for a video. Both mean Creator Inbox. Code that checks for the
literal `p_inbox_url~` will misread a perfectly good video post as having gone
to the public feed — match on `_inbox_url~` instead.

### The 90-character photo title

TikTok reuses `content` as the **slideshow title** for photo posts and caps it
at 90 characters. This appears on none of the Zernio docs pages above; it
surfaced only as a live 400, `TIKTOK_PHOTO_TITLE_TOO_LONG`, on a 99-char
caption. `publish.py` now derives a title (hashtags stripped, trimmed at a word
boundary) and sends the full caption in `tiktokSettings.description` instead,
which allows 4000. Video posts are not subject to the cap.

`tiktokSettings` is sent in **both** placements (top level and nested inside
`platformSpecificData`) with identical content. The platform-settings guide says
the nested copy "is not recognized"; the create-post schema shows both, and this
repo's history records a Creator Inbox landing while nested. Both cannot be
true, and the dangerous direction is losing the settings entirely - that drops
draft and privacy_level while publishNow still fires. Duplicating costs nothing.
Collapse to one placement only once a live run shows which copy is read.

### Carousel slides are not video slides

In a narrated video the voice carries the explanation, so the slide only needs
a headline. In a carousel **nothing is ever heard** — so `assemble.py` renders
its own slides with `include_narration=True`. Reusing the video slides would
ship the hook and silently drop every explanation.

### post.json declares its own blast radius

`privacy: SELF_ONLY`, `draft: true` and `approved_for_real_publish: false`
are written into the artifact, not left as a default inside `publish.py`. The
bundle states what it is allowed to become, so `publish.py` can assert on it
rather than assume. Each bundle also gets a `contact_sheet.png` showing every
slide in order — approving a post you cannot see is not approval, and that
sheet is what makes the phone gate meaningful.

### Timing is measured, not assumed

`voice.py` writes a manifest with each beat's real duration, taken from
edge-tts boundary events (no ffprobe needed). `assemble.py` should cut slides
against those numbers rather than any estimate.

`WORDS_PER_SEC` in `script_gen.py` is only for the pre-render estimate that
drives `format_hint`. It is calibrated to **3.3 w/s**, measured from 75 words
of `en-US-AndrewNeural` at `+0%` rendering to 22.5s of audio — currently within
1% of measured. MLTok's inherited 2.6 w/s overestimated runtime by 28%, enough
to push a script across the carousel/video boundary the wrong way.
**Re-measure and update it if the default voice or rate changes.**
