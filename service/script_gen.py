#!/usr/bin/env python3
"""
HackReel - script generation via the Google Gemini API.

Runtime component. No Claude Code, no skills, no terminal: this module is
imported by service/main.py and must work on a server with nothing but a
GEMINI_API_KEY in the environment.

Only the transport is Gemini-specific. The rubric, the story shapes, the
grounding rules and every deterministic guard below are provider-independent
and were carried over unchanged - they are the actual product, and swapping
model vendors must not quietly alter them.

Pipeline for one request:

    prompt -> [pass 1: writer] -> draft
           -> [style guard]                 (AI-cliche regex; one writer revision)
           -> [deterministic guards]        (regex + numeric grounding)
           -> [pass 2: critic]  -> verdict + revision, or a follow-up question
           -> [deterministic guards again]  (the critic does not get the last word)
           -> ok | need_more_info | error

The style guard is a separate concern from the rubric and must stay that way.
The rubric asks "is this true and specific?"; the style guard asks "does this
read like a machine wrote it?" A script can pass every rubric item, with every
number traceable, and still open with "it turns out". Truth and voice fail
independently, so they are checked independently.

Design notes worth keeping in mind before editing the prompts below:

* The rubric is a self-check, not a template. The model picks a STORY SHAPE
  that fits the project; the shape decides beat order and weight. The five
  semantic roles are a coverage checklist, not a fixed skeleton - a fixed
  skeleton is what makes generated video feel like generated video.
* The escape hatch is load-bearing. A thin prompt must produce a follow-up
  QUESTION, never invented specifics. Both passes can trigger it, and the
  deterministic guards can force it even when both passes think they passed.
* Narration (heard) and headline/sub (seen) are separate channels. Carried
  over from MLTok Agent: TikTok draft mode transfers media only, the caption
  never reaches the phone, so anything that must be read has to be drawn onto
  the slide by slides.py.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

import style_guard

# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------

# Free tier at https://aistudio.google.com needs no card.
#
# Do NOT "fix" this back to a 2.5 model: gemini-2.5-pro exhausts free quota and
# gemini-2.5-flash 404s on a free-tier key even though models.list() advertises
# it. An advertised model is not a callable model.
#
# Avoid the *-latest aliases: they float, and a graded demo wants a model that
# does not change underneath it.
#
# Measured on the real writer prompt, 2026-08-26: 3.6-flash 12.5s OK,
# 3-flash-preview 17.1s OK, 3.1-flash-lite 6.0s OK, 3.5-flash-lite 6.4s OK,
# while 3.5-flash and 3.7-flash both returned "high demand" 503s. The newest
# model is not the best default, and today's best default may 503 tomorrow -
# which is what FALLBACK_MODELS below is for.
MODEL = os.environ.get("HACKREEL_MODEL", "gemini-3.6-flash")

# Free-tier capacity moves around: on 2026-08-26, 3.5-flash and 3.7-flash both
# returned "high demand" 503s within minutes of 3.5-flash having worked fine,
# while 3.6-flash, 3-flash-preview and the lite models served the same prompt
# without complaint. Effort level made no difference - it is capacity, not load
# we generate.
#
# So do not pin one model and hope. If the primary is refusing, fail over
# rather than making a judge at a demo click Generate again. Ordered by
# measured quality-per-second on the real writer prompt; lites last because
# they think less and this task is a rubric judgement.
FALLBACK_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
MAX_TOKENS = 16000

# Free-tier flash models return transient 503 "high demand" under load. That is
# worth riding out rather than bouncing back to the user, who can only click the
# same button again. Retries happen per API CALL, not per generation: a failed
# critic pass must not re-run the writer pass, which would burn a second request
# of scarce free-tier quota and could return a different draft than the one the
# critic was judging.
RETRY_DELAYS = (2,)             # seconds; 2 attempts per model before failing over

# CRITICAL: without a per-request timeout, retries are worse than useless.
# Measured 2026-08-26: an overloaded gemini-3.7-flash hung for 263.8s before
# returning 503. Four attempts of that is ~17 minutes on the writer pass alone
# - far worse than failing once. A call that has not answered in this long is
# not going to; cut it and retry instead of waiting out the hang.
REQUEST_TIMEOUT_MS = 90_000

# The writer/critic passes reason about a rubric, so thinking is worth paying
# for. Gemini exposes discrete levels rather than a token budget.
EFFORT_TO_THINKING = {
    "low": "LOW", "medium": "MEDIUM", "high": "HIGH",
    "xhigh": "HIGH", "max": "HIGH",     # Gemini tops out at HIGH
}

# Beat-count and length bounds. These are enforced in code, not just asked for
# in the prompt - a script that violates them is rejected before it reaches TTS.
MIN_BEATS, MAX_BEATS = 4, 6
HOOK_MAX_WORDS = 16
BEAT_MAX_WORDS = 45
TOTAL_MIN_WORDS, TOTAL_MAX_WORDS = 55, 190
HEADLINE_MAX_CHARS = 24
SUB_MAX_CHARS = 46

# Measured, not assumed: 75 words of en-US-AndrewNeural at +0% rendered to
# 22.5s of real audio (3.33 w/s) across a 5-beat script. MLTok Agent's 2.6 w/s
# was inherited guesswork and overestimated runtime by 28%, which is enough to
# push a script across the carousel/video boundary in the wrong direction.
# Re-measure if the default voice or rate changes.
WORDS_PER_SEC = 3.3

# Below this, a narrated video is mostly slide with no motion; photo mode
# carries short text-forward content better.
CAROUSEL_MAX_SEC = 18.0

ROLES = ["hook", "what_it_does", "interesting_challenge", "demo_beat", "cta"]
SHAPE_IDS = ["problem_first", "almost_broke", "number_first",
             "wrong_assumption", "demo_cold_open"]


# ---------------------------------------------------------------------------
# The prompt: story shapes
# ---------------------------------------------------------------------------

SHAPES = """\
## Story shapes

Pick the ONE shape that genuinely fits this project. The shape decides the
order and the weight of the beats. Do not default to the first one; do not
average them together. If two fit, pick the one whose "use when" is more
literally true of the input.

**problem_first**
  Open on the pain the viewer already feels. Name it precisely enough that
  someone who has it says "that's me". Reveal the build as the answer, then
  show it working.
  Use when: the problem is common and the input describes it clearly.
  Avoid when: the pain is niche or the input never names who suffers from it.

**almost_broke**
  Open inside the failure - the thing that did not work, at the moment it did
  not work. The project is what survived it. The fix IS the story, so the
  challenge beat carries the most weight here.
  Use when: the input contains a real war story with a specific failure.
  Avoid when: you would have to invent the drama. This shape is a magnet for
  fabrication - if the input has no failure in it, choose another shape.

**number_first**
  Open on one surprising, concrete quantity, then earn it. Everything after
  the hook explains why that number is true.
  Use when: the input contains a real number that is genuinely surprising.
  Avoid when: the only numbers available are mundane (version numbers, team
  size, "24 hours" for a hackathon). Do not manufacture a statistic.

**wrong_assumption**
  Open on what everyone believed - including the team - then break it. The
  reveal is that the obvious approach was wrong.
  Use when: the input describes an approach that was tried and abandoned, or
  a surprising technical finding.
  Avoid when: the input is a straightforward "we built X that does Y".

**demo_cold_open**
  Open mid-action, narrating what is on screen as if the viewer already
  pressed play. Explanation comes after they are hooked by the motion.
  Use when: the demo is visually self-evident in one sentence.
  Avoid when: the project's value is conceptual and needs setup to land.
"""


# ---------------------------------------------------------------------------
# The prompt: rubric
# ---------------------------------------------------------------------------

RUBRIC = """\
## Rubric

Every script must pass all five. These are pass/fail, and the evidence must be
a quote from your own draft.

**R1 CONCRETE ANCHOR**
  At least one specific, checkable detail carried from the input: a number, an
  error message, a named tool or format, a particular moment. A capability
  claim is not an anchor. "It analyses your code" fails. "It reads the failing
  job log and points at one line of your diff" passes.

**R2 EARNED HOOK**
  The first line must be false of most other hackathon projects. If you could
  paste it onto a different project unchanged, it fails.
  Banned openers, no exceptions: "today I'll show you", "in this video",
  "have you ever wondered", "let me introduce", "imagine a world",
  "what if I told you", "in today's fast-paced world", "say hello to",
  "introducing", "we're excited to".

**R3 HONEST SCOPE**
  No result the input does not support. No invented metrics, benchmarks, user
  counts, funding, awards, or comparisons to named competitors. If the input
  hedges ("mostly works", "for Python repos"), the script hedges too. A
  prototype is described as a prototype.

**R4 EARNED MIDDLE**
  The challenge beat names a specific obstacle AND the shape of the fix.
  "Scaling was tricky" fails. "It kept citing files that didn't exist, so every
  claim now has to quote a real line from the diff" passes. If the input
  contains no such obstacle, this is a reason to ask a follow-up question, not
  a reason to invent one.

**R5 SPOKEN RHYTHM**
  It has to sound like a person talking, not a landing page. Sentences short
  enough to say in one breath. No marketing register: "revolutionary",
  "seamless", "game-changing", "leverage", "unlock", "empower", "supercharge",
  "cutting-edge", "next-level", "effortlessly".
"""


# ---------------------------------------------------------------------------
# The prompt: grounding + escape hatch (the anti-slop core)
# ---------------------------------------------------------------------------

GROUNDING = f"""\
## Grounding - the rule that outranks everything else

Every factual claim must trace to the input text. You may compress, reorder,
sharpen and dramatise what is there. You may not add.

Specifically forbidden, even when it would make a better video:
  - numbers that do not appear in the input (times, percentages, counts, users)
  - technologies, models, or frameworks the input does not mention
  - outcomes ("it found the bug in seconds") the input does not claim
  - a failure story the input does not describe

**If the input is too thin to pass the rubric honestly, do not pad it.**
Set `status` to `"need_more_info"` and write ONE specific follow-up question
that would unlock the beat you are missing. Padding a thin prompt with
plausible invention is the single worst failure mode of this system - it is
worse than returning nothing, because nobody catches it downstream.

A good question names the gap and gives the user an easy way in:
  - "What was the moment it almost didn't work? Even a small bug that cost you
     an hour gives the middle of the video something real to land on."
  - "What does it actually output - a number, a file, a comment on a PR? One
     concrete example makes the demo beat specific."
  - "Who has this problem today, and what do they do instead right now?"

A bad question is generic and puts the work back on the user:
  - "Can you tell me more about your project?"
  - "What else should I know?"

Ask for the ONE thing that would most improve the script. Never ask for more
than one thing. If the input is rich enough, do not ask at all - a needless
question is its own failure.

## Two channels

Each beat carries two independent pieces of text.
  - `narration`: what the voice SAYS. This is heard, never read.
  - `headline` + `sub`: what is DRAWN on the slide. This is read, never heard.

The headline is not a summary of the narration. It is the two or three words a
scrolling viewer catches at thumbnail size. Narration explains; the headline
stops the thumb. Never make them the same sentence.

`sub` is optional. It carries the qualifier the headline had to drop - the
unit, the caveat, the who. If the headline already stands alone, leave `sub`
empty rather than padding it with something to say.

## The closing beat

The last beat is the call to action, and it is the likeliest place in this
whole script for you to lie. Do not invent a destination.

If the input names a repo, a URL, a handle, a waitlist or a launch date, you
may point at it. If it does not, close on something else that is true: what
happens next, what is still broken, what one more day would buy. "Link in bio"
is allowed only when the input says there is a link.

A closing beat that promises a destination the project does not have is the
failure a viewer notices personally, because they go looking for it.

## Naming

`project` is the name exactly as the input gives it. If the input never names
the project, use a plain description like "this build" - never coin a product
name. A name you invented will be spoken aloud by the voice and burned into
the slides as though it were real.

## Hard limits (violations are rejected in code, before this reaches a voice)

  - {MIN_BEATS}-{MAX_BEATS} beats total
  - first beat: at most {HOOK_MAX_WORDS} words of narration
  - any beat: at most {BEAT_MAX_WORDS} words of narration
  - whole script: {TOTAL_MIN_WORDS}-{TOTAL_MAX_WORDS} words (~{round(TOTAL_MIN_WORDS / WORDS_PER_SEC)}-{round(TOTAL_MAX_WORDS / WORDS_PER_SEC)} seconds)
  - `headline`: at most {HEADLINE_MAX_CHARS} characters, uppercase
  - `sub`: at most {SUB_MAX_CHARS} characters, may be empty
  - roles used across beats must come from: {", ".join(ROLES)}
  - `hook` and `cta` appear exactly once: the first beat and the last beat
  - any other role may appear at most twice
"""


WRITER_SYSTEM = f"""\
You write the script for a 30-60 second vertical video about a hackathon
project. It will be narrated by a synthetic voice over full-screen text
slides, and posted to TikTok.

Your audience is other builders scrolling fast. They have seen a thousand
project demos. They can smell a generated script instantly, and the tell is
always the same: structural sameness, and specifics that turn out to be
generic on a second read.

{SHAPES}
{RUBRIC}
{GROUNDING}

## Process

1. Read the input and list, to yourself, the concrete specifics actually in it.
2. If there are none worth building on, stop and return `need_more_info`.
3. Choose the story shape those specifics support. Record it in `shape`, and in
   `shape_reason` name the particular detail in the input that drove the
   choice - quote it. "This project has a clear problem" is not a reason;
   "the input says the log is 4000 lines and nobody reads it" is.
4. Write the beats in the order the shape dictates.
5. Check your draft against R1-R5. Fix what fails before you answer.

Return `status: "ok"` with the script, or `status: "need_more_info"` with a
question and no script. Never both.
"""


CRITIC_SYSTEM = f"""\
You are the second pass over a draft script for a short vertical video about a
hackathon project. The first pass has already run and believes it succeeded.

Your stance is adversarial. Assume the draft is generic until a specific quote
from it proves otherwise. The first pass grades itself generously; you exist
because of that.

You will be given the ORIGINAL INPUT and the DRAFT. Judge the draft against
the rubric, using only the original input as the source of truth.

{RUBRIC}
{GROUNDING}

## What you must do

1. Score R1-R5. For each, `passed` true/false and `evidence` quoting the draft
   verbatim - the line that earns the pass, or the line that fails it. Never
   cite a line that is not in the draft.
2. Check every claim in the draft against the original input. Anything you
   cannot trace, list in `ungrounded_claims` verbatim.
3. Then choose exactly one outcome:

   **`revised`** - the draft mostly works. Return the full script with the
   failing parts rewritten. Leave passing beats ALONE; do not restyle what
   already works. Gratuitous rewriting loses the specifics the first pass got
   right, which is a regression even when the prose reads better.

   **`unchanged`** - the draft passes all five cleanly. Return it as-is. This
   is a real outcome, not a cop-out. Do not invent problems to look useful.

   **`need_more_info`** - the draft cannot be fixed from the input available.
   Return the follow-up question and no script.

## The rule that decides between `revised` and `need_more_info`

If a criterion fails because the DRAFT is weak but the input has the material,
fix it -> `revised`.

If a criterion fails because the INPUT lacks the material, you must return
`need_more_info`. You may NOT repair a grounding failure by supplying the
missing detail yourself. A rubric pass bought with an invented number is the
exact outcome this second pass exists to prevent. When you are tempted to
write a specific that would make the script work, that temptation IS the
signal to ask the user instead.
"""


# ---------------------------------------------------------------------------
# Output schemas (hand-written: flat, no $ref, so the API's json_schema
# validator and this file never disagree about the shape)
# ---------------------------------------------------------------------------

_BEAT_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string", "enum": ROLES},
        "headline": {"type": "string",
                     "description": f"Uppercase, <= {HEADLINE_MAX_CHARS} chars. Read, not heard."},
        "sub": {"type": "string",
                "description": f"Supporting slide line, <= {SUB_MAX_CHARS} chars. May be empty."},
        "narration": {"type": "string",
                      "description": f"Spoken aloud. <= {BEAT_MAX_WORDS} words."},
    },
    "required": ["role", "headline", "sub", "narration"],
    "additionalProperties": False,
}

_SCRIPT_PROPS = {
    "project": {"type": "string", "description": "Project name as given in the input."},
    "shape": {"type": "string", "enum": SHAPE_IDS},
    "shape_reason": {"type": "string", "description": "One sentence: why this shape fits."},
    "beats": {"type": "array", "items": _BEAT_SCHEMA,
              "minItems": MIN_BEATS, "maxItems": MAX_BEATS},
    "caption": {"type": "string", "description": "TikTok caption. Not spoken, not drawn."},
    "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
}

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "need_more_info"]},
        "question": {"type": "string",
                     "description": "Set only when status is need_more_info; else empty."},
        "script": {
            "type": "object",
            "properties": _SCRIPT_PROPS,
            "required": list(_SCRIPT_PROPS),
            "additionalProperties": False,
        },
    },
    "required": ["status", "question", "script"],
    "additionalProperties": False,
}

_RUBRIC_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "enum": ["R1", "R2", "R3", "R4", "R5"]},
        "passed": {"type": "boolean"},
        "evidence": {"type": "string", "description": "Verbatim quote from the draft."},
    },
    "required": ["id", "passed", "evidence"],
    "additionalProperties": False,
}

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["unchanged", "revised", "need_more_info"]},
        "rubric": {"type": "array", "items": _RUBRIC_ITEM, "minItems": 5, "maxItems": 5},
        "ungrounded_claims": {"type": "array", "items": {"type": "string"}},
        "question": {"type": "string",
                     "description": "Set only when outcome is need_more_info; else empty."},
        "script": {
            "type": "object",
            "properties": _SCRIPT_PROPS,
            "required": list(_SCRIPT_PROPS),
            "additionalProperties": False,
        },
    },
    "required": ["outcome", "rubric", "ungrounded_claims", "question", "script"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Deterministic guards - these do not trust either model pass
# ---------------------------------------------------------------------------

# Distinctive enough to ban anywhere in the opening beat - "So, today I'll
# show you..." must not slip through on a startswith check.
BANNED_ANYWHERE = [
    "today i'll show you", "today i will show you", "in this video",
    "have you ever wondered", "let me introduce", "imagine a world",
    "what if i told you", "in today's fast-paced world", "say hello to",
    "we're excited to", "we are excited to",
]
# Ambiguous as a substring ("we kept re-introducing the bug"), so only banned
# when the hook actually opens on it.
BANNED_AT_START = ["introducing"]

# MARKETING_WORDS used to live here and hard-failed a script to
# need_more_info. That was the wrong response to a phrasing problem: bad
# wording is fixable by revision, not a reason to give up and interrogate the
# user about facts that were never missing. Those terms now live in
# style_guard.RULES, which sends the specific phrase back to the writer.
# Do not reintroduce a second copy here - two mechanisms reacting to the same
# word with different outcomes is how a script gets rejected for a reason
# nobody can explain.

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "fifteen": "15", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
    "hundred": "100", "thousand": "1000", "million": "1000000",
}


def _words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _numbers_in(text: str) -> set[str]:
    """Digit-forms present in a text, counting spelled-out numbers too, so that
    an input saying 'thirty hours' grounds an output saying '30 hours'."""
    low = (text or "").lower()
    found = {n.replace(",", "") for n in _NUM_RE.findall(low)}
    for word, digits in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            found.add(digits)
    return found


def ungrounded_numbers(script: dict, source: str) -> list[str]:
    """Digits that appear in the script but trace to nothing in the input.

    Invented metrics are the most damaging hallucination this system can emit
    and the cheapest to catch exactly, so it is checked in code rather than
    left to the critic's judgement. Only digit forms in the OUTPUT are checked;
    spelled-out small numbers ('one comment') are prose, not claims.
    """
    allowed = _numbers_in(source)
    bad = []
    for beat in script.get("beats", []):
        for field_name in ("narration", "headline", "sub"):
            for num in _NUM_RE.findall(beat.get(field_name, "")):
                clean = num.replace(",", "")
                if clean not in allowed and clean not in bad:
                    bad.append(clean)
    return bad


def check_script(script: dict, source: str) -> list[str]:
    """Hard structural + honesty checks. A non-empty return means do not ship."""
    errs: list[str] = []
    beats = script.get("beats") or []

    if not (MIN_BEATS <= len(beats) <= MAX_BEATS):
        errs.append(f"{len(beats)} beats, must be {MIN_BEATS}-{MAX_BEATS}")
        return errs

    if beats[0].get("role") != "hook":
        errs.append(f"first beat role is {beats[0].get('role')!r}, must be 'hook'")
    if beats[-1].get("role") != "cta":
        errs.append(f"last beat role is {beats[-1].get('role')!r}, must be 'cta'")

    counts = Counter(b.get("role") for b in beats)
    for role in ("hook", "cta"):
        if counts.get(role, 0) != 1:
            errs.append(f"role {role!r} appears {counts.get(role, 0)} times, must be exactly 1")
    for role, n in counts.items():
        if role not in ("hook", "cta") and n > 2:
            errs.append(f"role {role!r} appears {n} times, max 2")

    total = 0
    for i, b in enumerate(beats):
        n, h, s = b.get("narration", ""), b.get("headline", ""), b.get("sub", "")
        w = _words(n)
        total += w
        cap = HOOK_MAX_WORDS if i == 0 else BEAT_MAX_WORDS
        if w > cap:
            errs.append(f"beat {i} ({b.get('role')}) is {w} words, max {cap}")
        if not h.strip():
            errs.append(f"beat {i} has no headline (slides carry the only readable text)")
        if len(h) > HEADLINE_MAX_CHARS:
            errs.append(f"beat {i} headline is {len(h)} chars, max {HEADLINE_MAX_CHARS}: {h!r}")
        if len(s) > SUB_MAX_CHARS:
            errs.append(f"beat {i} sub is {len(s)} chars, max {SUB_MAX_CHARS}")

    if not (TOTAL_MIN_WORDS <= total <= TOTAL_MAX_WORDS):
        errs.append(f"script is {total} words, must be {TOTAL_MIN_WORDS}-{TOTAL_MAX_WORDS}")

    opener = (beats[0].get("narration", "") or "").lower().lstrip("\"' ")
    hit = next((p for p in BANNED_ANYWHERE if p in opener), None) \
        or next((p for p in BANNED_AT_START if opener.startswith(p)), None)
    if hit:
        errs.append(f"hook uses banned opener: {hit!r}")

    # Marketing register is checked by style_guard, not here - see the note at
    # MARKETING_WORDS above. This function stays about structure and truth.

    bad_nums = ungrounded_numbers(script, source)
    if bad_nums:
        errs.append("numbers not traceable to the input: " + ", ".join(bad_nums))

    return errs


def load_script_file(path: str) -> dict:
    """Read a script from disk, accepting either a bare script object or a
    full /api/generate result with the script nested under `script`."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    script = data.get("script", data)
    if not script.get("beats"):
        raise ValueError(f"{path} has no beats - is it a HackReel script?")
    return script


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (name or "script")).strip("-").lower()
    return s[:40] or "script"


def est_duration(script: dict) -> float:
    """Pre-render estimate only. voice.py measures the real durations from
    edge-tts boundary events and writes them to its manifest; prefer those
    once audio exists."""
    total = sum(_words(b.get("narration", "")) for b in script.get("beats", []))
    return round(total / WORDS_PER_SEC, 1)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

def describe_error(exc: BaseException) -> tuple[str, bool]:
    """(human message, retryable). Duck-typed on the SDK's exception surface so
    this module still imports when `google-genai` is absent. The UI needs the
    retryable flag to know whether to offer 'try again' or 'fix your key'."""
    name = type(exc).__name__
    # google.genai.errors.APIError carries .code; fall back to .status_code.
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return ("rate limited or out of free-tier quota - wait and retry, or set "
                "HACKREEL_MODEL to a lighter flash-lite model"), True
    if name in ("ServerError", "ConnectionError", "TimeoutError") or \
            (isinstance(code, int) and code >= 500):
        return f"Gemini API server error ({code or name})", True
    if code == 401:
        return "GEMINI_API_KEY was rejected", False
    if code == 403:
        return "this API key lacks permission for the requested model", False
    if code == 404:
        return f"model {MODEL!r} is not available to this key", False
    if code == 400:
        return f"bad request: {getattr(exc, 'message', None) or exc}", False
    return str(getattr(exc, "message", None) or exc) or name, False


@dataclass
class GenerationResult:
    status: Literal["ok", "need_more_info", "error"]
    script: dict | None = None
    question: str = ""
    error: str = ""
    retryable: bool = False
    rubric: list[dict] = field(default_factory=list)
    ungrounded_claims: list[str] = field(default_factory=list)
    critic_outcome: str = ""
    style_flags: list = field(default_factory=list)
    style_revised: bool = False
    guard_errors: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "status": self.status,
            "question": self.question,
            "error": self.error,
            "retryable": self.retryable,
            "rubric": self.rubric,
            "ungrounded_claims": self.ungrounded_claims,
            "critic_outcome": self.critic_outcome,
            "style_flags": self.style_flags,
            "style_revised": self.style_revised,
            "guard_errors": self.guard_errors,
            "usage": self.usage,
        }
        if self.script:
            est = est_duration(self.script)
            d["script"] = self.script
            d["est_duration_sec"] = est
            d["format_hint"] = "carousel" if est < CAROUSEL_MAX_SEC else "video"
        return d


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def has_api_key() -> bool:
    """The SDK accepts either name; report on both so the UI does not warn
    about a missing key that is actually present under the other one."""
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def _client():
    # Imported lazily so --show-prompt and the guards work without the SDK.
    from google import genai
    from google.genai import types

    if not has_api_key():
        raise RuntimeError(
            "GEMINI_API_KEY is not set. HackReel reads it from the environment; "
            "never hardcode it into a file. Get a free key (no card) at "
            "https://aistudio.google.com/apikey"
        )
    return genai.Client(
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))


def _call(client, system: str, user: str, schema: dict, effort: str,
          model: str | None = None) -> tuple[dict, dict]:
    """One structured-output request. Returns (parsed_json, usage)."""
    from google.genai import types

    resp = client.models.generate_content(
        model=model or MODEL,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=MAX_TOKENS,
            response_mime_type="application/json",
            # response_json_schema takes standard JSON Schema, so the same
            # schema objects the guards are written against go straight in -
            # no OpenAPI-subset rewrite, nothing to drift out of sync.
            response_json_schema=schema,
            thinking_config=types.ThinkingConfig(
                thinking_level=EFFORT_TO_THINKING.get(effort, "HIGH")),
            # We pass no tools; without this the SDK logs an automatic-function-
            # calling warning on every single call.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True),
        ),
    )

    # A blocked or truncated response has no usable JSON. Name the reason
    # rather than dying later on a confusing parse error.
    cand = (resp.candidates or [None])[0]
    finish = getattr(cand, "finish_reason", None)
    finish_name = getattr(finish, "name", None) or (str(finish) if finish else "")
    if finish_name in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
        raise RuntimeError(f"the model blocked this request ({finish_name})")
    if finish_name == "MAX_TOKENS":
        raise RuntimeError(
            "response hit max_output_tokens before finishing - the JSON is truncated")

    text = resp.text
    if not text:
        raise RuntimeError(f"empty response (finish_reason={finish_name or 'unknown'})")

    u = resp.usage_metadata
    usage = {
        "input_tokens": getattr(u, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(u, "candidates_token_count", 0) or 0,
        "thinking_tokens": getattr(u, "thoughts_token_count", 0) or 0,
        "request_id": getattr(resp, "response_id", "") or "",
    }
    return json.loads(text), usage


def _call_resilient(client, system: str, user: str, schema: dict, effort: str,
                    sleep=time.sleep) -> tuple[dict, dict]:
    """Try the primary model, then fail over through FALLBACK_MODELS.

    Free-tier capacity fluctuates per model minute to minute, so a 503 says
    nothing about whether a sibling model will serve the same prompt. Only
    retryable failures trigger failover - a bad schema would fail identically
    on every model and must surface at once.
    """
    chain = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    last: Exception | None = None
    for i, model in enumerate(chain):
        try:
            data, usage = _call_with_retry(client, system, user, schema, effort,
                                           sleep=sleep, model=model)
            usage["model"] = model
            usage["failed_over"] = i > 0
            return data, usage
        except Exception as e:  # noqa: BLE001
            _, retryable = describe_error(e)
            if not retryable:
                raise
            last = e
    raise last if last else RuntimeError("no models configured")


def _call_with_retry(client, system: str, user: str, schema: dict, effort: str,
                     sleep=time.sleep, model: str | None = None) -> tuple[dict, dict]:
    """_call, but riding out transient overload.

    Only errors describe_error() calls retryable are retried - a rejected key,
    a bad schema or a safety block will never succeed on a second attempt, and
    retrying them just makes the user wait longer for the same failure.
    """
    attempts = 0
    for delay in (None,) + RETRY_DELAYS:
        if delay is not None:
            # Jitter so repeated clicks do not resynchronise onto the same
            # retry instant and hit the overloaded model together.
            sleep(delay + random.uniform(0, 0.75))
        attempts += 1
        try:
            data, usage = _call(client, system, user, schema, effort, model=model)
            usage["attempts"] = attempts
            return data, usage
        except Exception as e:  # noqa: BLE001
            _, retryable = describe_error(e)
            if not retryable or delay == RETRY_DELAYS[-1]:
                raise
    raise RuntimeError("unreachable")


def _merge_usage(a: dict, b: dict) -> dict:
    prior = a.get("request_ids") or [x for x in [a.get("request_id")] if x]
    return {
        "input_tokens": a.get("input_tokens", 0) + b.get("input_tokens", 0),
        "output_tokens": a.get("output_tokens", 0) + b.get("output_tokens", 0),
        "thinking_tokens": a.get("thinking_tokens", 0) + b.get("thinking_tokens", 0),
        "attempts": a.get("attempts", 0) + b.get("attempts", 0),
        "models": [m for m in (a.get("models") or [a.get("model")]) + [b.get("model")] if m],
        "request_ids": prior + [x for x in [b.get("request_id")] if x],
    }


def generate(prompt: str, *, effort: str = "high", client: Any = None) -> GenerationResult:
    """Free-text project description -> validated script, or a follow-up question.

    Never raises for ordinary failure. Callers branch on `.status`.
    """
    prompt = (prompt or "").strip()
    if len(prompt) < 20:
        return GenerationResult(
            status="need_more_info",
            question=("Tell me what you built and who it's for - a couple of sentences "
                      "is enough to start. What does it actually do?"),
        )

    try:
        client = client or _client()
    except RuntimeError as e:
        return GenerationResult(status="error", error=str(e))

    usage: dict = {}

    # ---- pass 1: write -----------------------------------------------------
    try:
        draft, u1 = _call_resilient(client, WRITER_SYSTEM,
                                    f"PROJECT DESCRIPTION:\n\n{prompt}",
                                    WRITER_SCHEMA, effort)
        usage = _merge_usage(usage, u1)
    except Exception as e:  # network, refusal, malformed JSON
        msg, retry = describe_error(e)
        return GenerationResult(status="error", error=f"draft pass failed: {msg}",
                                retryable=retry, usage=usage)

    if draft.get("status") == "need_more_info":
        return GenerationResult(
            status="need_more_info",
            question=draft.get("question") or "Can you add one concrete detail about how it works?",
            usage=usage,
        )

    draft_script = draft.get("script") or {}

    # ---- style pass: AI-cliche phrasing, sent back to the WRITER -----------
    # Deliberately before the critic. The critic judges truth and specificity;
    # handing it prose that still says "it turns out" wastes its attention on
    # a problem a regex already localised exactly. Capped at ONE revision: a
    # second attempt on a phrase the writer would not drop costs free-tier
    # quota to re-litigate taste, and the flags are reported either way.
    style_flags = style_guard.check(draft_script)
    style_revised = False
    if style_flags:
        try:
            revised, u_style = _call_resilient(
                client, WRITER_SYSTEM,
                f"PROJECT DESCRIPTION:\n\n{prompt}\n\n"
                f"YOUR PREVIOUS DRAFT:\n\n{json.dumps(draft_script, indent=2)}\n\n"
                f"{style_guard.revision_note(style_flags)}",
                WRITER_SCHEMA, effort)
            usage = _merge_usage(usage, u_style)
            if revised.get("status") == "ok" and revised.get("script"):
                draft_script = revised["script"]
                style_revised = True
        except Exception:
            # A style revision is an improvement, not a requirement. If the
            # model is unreachable, keep the grounded draft and report the
            # flags rather than losing a valid script to a cosmetic retry.
            pass
        style_flags = style_guard.check(draft_script)

    # ---- pass 2: critique + revise -----------------------------------------
    guard_before = check_script(draft_script, prompt)
    critic_user = (
        f"ORIGINAL INPUT:\n\n{prompt}\n\n"
        f"DRAFT:\n\n{json.dumps(draft_script, indent=2)}\n"
    )
    if guard_before:
        # Hand the critic what the deterministic checks already caught, so its
        # revision fixes them instead of rediscovering them by chance.
        critic_user += (
            "\nAUTOMATED CHECKS ALREADY FAILED ON THIS DRAFT - your revision must "
            "resolve every one of these, without inventing anything to do it:\n  - "
            + "\n  - ".join(guard_before) + "\n"
        )

    try:
        crit, u2 = _call_resilient(client, CRITIC_SYSTEM, critic_user,
                                   CRITIC_SCHEMA, effort)
        usage = _merge_usage(usage, u2)
    except Exception as e:
        msg, retry = describe_error(e)
        return GenerationResult(status="error", error=f"critique pass failed: {msg}",
                                retryable=retry, usage=usage)

    outcome = crit.get("outcome", "")
    rubric = crit.get("rubric", [])
    ungrounded = crit.get("ungrounded_claims", [])

    if outcome == "need_more_info":
        return GenerationResult(
            status="need_more_info",
            question=crit.get("question") or "What was the hardest part to get working?",
            rubric=rubric, ungrounded_claims=ungrounded,
            critic_outcome=outcome, usage=usage,
        )

    final = crit.get("script") or draft_script

    # ---- final guards: the critic does not get the last word ---------------
    guard_after = check_script(final, prompt)
    if guard_after:
        return GenerationResult(
            status="need_more_info",
            question=(crit.get("question") or
                      "I couldn't get this to a script I'd stand behind from the detail "
                      "available. What's one specific thing that happened while building "
                      "it - a bug, a number, a moment it nearly didn't work?"),
            rubric=rubric, ungrounded_claims=ungrounded,
            critic_outcome=outcome, guard_errors=guard_after, usage=usage,
        )

    if ungrounded:
        # The critic itself flagged untraceable claims but still returned a
        # script. Trust the flag over the outcome and refuse to ship.
        return GenerationResult(
            status="need_more_info",
            question=("Some of this didn't trace back to what you told me: "
                      + "; ".join(ungrounded[:2])
                      + ". Can you confirm those, or give me the real details?"),
            rubric=rubric, ungrounded_claims=ungrounded,
            critic_outcome=outcome, usage=usage,
        )

    return GenerationResult(status="ok", script=final, rubric=rubric,
                            critic_outcome=outcome, usage=usage,
                            style_flags=style_guard.check(final),
                            style_revised=style_revised)


# ---------------------------------------------------------------------------
# CLI - inspect the prompts, or run a generation
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="HackReel script generator")
    ap.add_argument("--show-prompt", choices=["writer", "critic", "both"],
                    help="print the system prompt(s) and exit (no API call)")
    ap.add_argument("--prompt", help="project description")
    ap.add_argument("--file", help="read the project description from a file")
    ap.add_argument("--effort", default="high",
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--out", help="write the result JSON here")
    a = ap.parse_args()

    if a.show_prompt:
        if a.show_prompt in ("writer", "both"):
            print("=" * 72); print("WRITER SYSTEM PROMPT"); print("=" * 72)
            print(WRITER_SYSTEM)
        if a.show_prompt in ("critic", "both"):
            print("=" * 72); print("CRITIC SYSTEM PROMPT"); print("=" * 72)
            print(CRITIC_SYSTEM)
        raise SystemExit(0)

    text = ""
    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            text = fh.read()
    elif a.prompt:
        text = a.prompt
    else:
        ap.error("need --prompt, --file, or --show-prompt")

    result = generate(text, effort=a.effort)
    payload = json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
    print(payload)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
