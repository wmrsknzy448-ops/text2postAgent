#!/usr/bin/env python3
"""
HackReel - style guard. Deterministic AI-cliche detection.

This is NOT the rubric and NOT the grounding check. Those ask "is this true?"
and "is this specific?". This asks a different question entirely: "does this
READ like it was written by a language model?" A script can be perfectly
grounded, every number traceable, every claim honest - and still announce
itself as machine-written in the first three words.

Deterministic by design. No model judges this: a model asked whether its own
prose sounds like AI will say no, and an LLM judge here would be one more
thing to be wrong. Regex is dumb, but it is dumb the same way every time.

When something is flagged the fix is NOT to auto-rewrite it and NOT to tell
the writer to "sound more natural" - vague instructions produce vague edits.
The flagged phrase is named, verbatim, and sent back so the revision is
deliberate.

    python style_guard.py --list          # print the rules for review
    python style_guard.py --test          # run against the built-in samples
    python style_guard.py script.json     # check a real script
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# THE RULE TABLE - this is the part to edit.
#
# (id, pattern, what it catches, why it reads as AI)
# Patterns are case-insensitive. Add, delete or loosen freely; nothing below
# this table hardcodes any particular rule.
# ---------------------------------------------------------------------------

RULES: list[tuple[str, str, str, str]] = [
    (
        "connector-dash",
        r"\w\s*[—–]\s*\w",
        "em/en dash used as a connector",
        "The em-dash aside is the single most recognisable tell. It also reads "
        "badly aloud - TTS either ignores it or pauses wrongly - and it eats "
        "width on a slide.",
    ),
    (
        "not-just-but",
        r"\bnot\s+(?:just|only|merely|simply)\b[^.!?]{0,60}?\bbut\b",
        "\"not just X but Y\" construction",
        "The signature LLM escalation. Almost never how a person says it.",
    ),
    (
        "isnt-just-its",
        r"\b(?:is|it)\s?n[o']?t\s+(?:just|only)\b[^.!?]{0,60}?,\s*it'?s\b",
        "\"isn't just X, it's Y\" construction",
        "Same escalation, different clothes.",
    ),
    (
        "seamlessly",
        r"\bseamless(?:ly)?\b",
        "\"seamless\" / \"seamlessly\"",
        "Nothing a human built in thirty hours is seamless.",
    ),
    (
        "game-changer",
        r"\bgame[\s\-]?chang(?:er|ing|ers)\b",
        "\"game-changer\" / \"game-changing\"",
        "Pure marketing register; claims impact instead of showing it.",
    ),
    (
        "in-todays-world",
        r"\bin\s+today'?s\s+(?:world|landscape|market|digital\s+age|"
        r"fast[\s\-]?paced\s+world)\b",
        "\"in today's world\" and variants",
        "Opens on a generality that fits any topic - the opposite of a hook.",
    ),
    (
        "leverage-verb",
        r"\b(?:to|we|you|they|it|can|could|will|would|should|helps?|lets?|and)"
        r"\s+leverage\b|\bleverag(?:es|ing|ed)\b",
        "\"leverage\" used as a verb",
        "Consultant-speak for \"use\". The noun (\"financial leverage\") is "
        "left alone deliberately.",
    ),
    (
        "unlock",
        r"\bunlock(?:s|ed|ing)?\b",
        "\"unlock\"",
        "Promises a payoff without naming it.",
    ),
    (
        "elevate",
        r"\belevat(?:e|es|ed|ing)\b",
        "\"elevate\"",
        "Same - abstract uplift, no content.",
    ),
    (
        "it-turns-out",
        r"\bit\s+turns\s+out\b",
        "\"it turns out\"",
        "The AI punchline pivot: a fake reveal that signals a tidy lesson is "
        "coming. Real findings get stated, not announced.",
    ),
]

RULES += [
    # --- previously "suggested", enabled 2026-08-31 at the operator's request
    ("delve", r"\bdelv(?:e|es|ing)\b", "\"delve into\"",
     "Vanishingly rare in speech; near-diagnostic of generated text."),
    ("deep-dive", r"\bdeep[\s\-]?dive\b", "\"deep dive\"",
     "Content-marketing filler for \"look at\"."),
    ("revolutionize", r"\brevolutioni[sz](?:e|es|ing|ed)\b", "\"revolutionise\"",
     "Claims scale a hackathon build cannot support."),
    ("supercharge", r"\bsupercharg(?:e|es|ing|ed)\b", "\"supercharge\"",
     "Intensity without information."),
    ("empower", r"\bempower(?:s|ing|ed)?\b", "\"empower\"",
     "Abstract uplift; says nothing about what the thing does."),
    ("streamline", r"\bstreamlin(?:e|es|ing|ed)\b", "\"streamline\"",
     "Consultant register for \"make faster\"."),
    ("harness", r"\bharness(?:es|ing|ed)?\s+the\s+power\b", "\"harness the power\"",
     "Pure boilerplate."),
    ("cutting-edge", r"\bcutting[\s\-]?edge\b", "\"cutting-edge\"",
     "Self-congratulation in place of a specific."),
    ("at-the-end", r"\bat\s+the\s+end\s+of\s+the\s+day\b",
     "\"at the end of the day\"", "Filler that delays the point."),
    ("little-did-i", r"\blittle\s+did\s+(?:i|we)\s+know\b", "\"little did I know\"",
     "Storybook foreshadowing; signals a manufactured arc."),
    ("heres-the-kicker", r"\bhere'?s\s+the\s+kicker\b", "\"here's the kicker\"",
     "Announces a payoff instead of delivering one."),
    ("thats-when-it-hit", r"\bthat'?s\s+when\s+it\s+hit\s+(?:me|us)\b",
     "\"that's when it hit me\"", "Same manufactured epiphany beat."),
    ("secret-sauce", r"\bsecret\s+sauce\b", "\"the secret sauce\"",
     "Promises a mechanism while withholding it."),
    ("next-level", r"\b(?:to\s+the\s+)?next[\s\-]?level\b", "\"next level\"",
     "Comparative with nothing to compare to."),
    ("let-that-sink-in", r"\blet\s+that\s+sink\s+in\b", "\"let that sink in\"",
     "Instructs the viewer how to feel."),
    ("testament", r"\ba\s+testament\s+to\b", "\"a testament to\"",
     "Editorialises instead of showing the thing."),

    # --- absorbed from script_gen.MARKETING_WORDS, which is now retired.
    # These previously hard-failed a script to need_more_info, which was the
    # wrong response: phrasing is fixable by revision, not a reason to give up
    # and interrogate the user. The style guard is now their single home.
    ("revolutionary", r"\brevolutionary\b", "\"revolutionary\"",
     "Adjective form of the same overclaim."),
    ("effortless", r"\beffortless(?:ly)?\b", "\"effortless\" / \"effortlessly\"",
     "Nothing built under deadline is effortless."),
]

FIELDS = ("narration", "headline", "sub")


# ---------------------------------------------------------------------------

def _compiled():
    return [(rid, re.compile(pat, re.I), desc, why) for rid, pat, desc, why in RULES]


def check(script: dict) -> list[dict]:
    """Return one finding per (rule, beat, field) hit.

    Each finding names the phrase verbatim so the revision request can quote
    it back instead of gesturing at a vibe.
    """
    out: list[dict] = []
    for i, beat in enumerate(script.get("beats", [])):
        for field in FIELDS:
            text = beat.get(field) or ""
            if not text:
                continue
            for rid, rx, desc, why in _compiled():
                for m in rx.finditer(text):
                    # A bare match is useless for some rules - the dash rule
                    # matches "s — c", which tells the writer nothing. Quote a
                    # window around it so the revision request points at a
                    # phrase a human can actually find in their own sentence.
                    lo = max(0, m.start() - 30)
                    hi = min(len(text), m.end() + 30)
                    context = ("…" if lo else "") + text[lo:hi].strip() + \
                              ("…" if hi < len(text) else "")
                    out.append({
                        "rule": rid,
                        "beat": i,
                        "role": beat.get("role", ""),
                        "field": field,
                        "match": m.group(0).strip(),
                        "context": context,
                        "description": desc,
                        "why": why,
                    })
    return out


def revision_note(findings: list[dict]) -> str:
    """The message handed back to the WRITER pass.

    Names each phrase and where it sits. Deliberately does not suggest a
    replacement: proposing the fix here would just swap one canned phrase for
    another, and the writer has the context to re-say the line properly.
    """
    if not findings:
        return ""
    lines = [
        "STYLE REVISION REQUIRED. Your draft is factually fine; these phrases "
        "read as machine-written and must be rewritten:",
        "",
    ]
    seen: set[tuple] = set()
    for f in findings:
        key = (f["rule"], f["beat"], f["field"], f["match"].lower())
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"  - beat {f['beat']} ({f['role']}), {f['field']}: \"{f['match']}\"\n"
            f"      in: \"{f['context']}\"\n"
            f"      {f['description']} - {f['why']}"
        )
    lines += [
        "",
        "Rewrite ONLY these phrases. Say the same thing the way a person would "
        "say it out loud. Keep every fact, number and specific exactly as it "
        "is - this is a phrasing problem, not a content problem. Do not "
        "substitute a different stock phrase.",
    ]
    return "\n".join(lines)


def summarize(findings: list[dict]) -> str:
    if not findings:
        return "clean - no AI-cliche phrasing detected"
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    return f"{len(findings)} flag(s): " + ", ".join(
        f"{k}x{v}" if v > 1 else k for k, v in sorted(by_rule.items()))


# ---------------------------------------------------------------------------

SAMPLES = {
    "the line you flagged": {"beats": [{
        "role": "cta", "headline": "NO MORE SLOP", "sub": "",
        "narration": "It turns out AI scripting only works when you build an "
                     "AI that rejects AI nonsense."}]},
    "quoted headlines": {"beats": [
        {"role": "hook", "headline": "AI SCRIPT FAIL", "sub": "", "narration": "x"},
        {"role": "what_it_does", "headline": "WRITER VS CRITIC", "sub": "", "narration": "y"},
        {"role": "cta", "headline": "NO MORE SLOP", "sub": "", "narration": "z"},
    ]},
    "em-dash connector": {"beats": [{
        "role": "hook", "headline": "OK", "sub": "",
        "narration": "The model kept inventing paths — confident, plausible, "
                     "and completely fake."}]},
    "not just / but": {"beats": [{
        "role": "hook", "headline": "OK", "sub": "",
        "narration": "This is not just a linter but a full triage bot."}]},
    "marketing pile-up": {"beats": [{
        "role": "hook", "headline": "OK", "sub": "seamlessly elevate",
        "narration": "In today's world you can leverage AI to unlock a "
                     "game-changing workflow."}]},
    "clean control": {"beats": [{
        "role": "hook", "headline": "YOUR CI IS RED", "sub": "4000 lines",
        "narration": "Your build is red and the log is four thousand lines of "
                     "nothing useful."}]},
}


if __name__ == "__main__":
    if "--list" in sys.argv:
        print(f"{len(RULES)} ACTIVE RULES\n" + "=" * 72)
        for rid, pat, desc, why in RULES:
            print(f"\n{rid}\n  catches : {desc}\n  why     : {why}\n  regex   : {pat}")
        print("\n\n" + "=" * 72)
        print(f"{len(SUGGESTED)} SUGGESTED, NOT ACTIVE (move into RULES to enable)")
        print("=" * 72)
        for rid, pat, desc in SUGGESTED:
            print(f"  {rid:20s} {desc}")
        raise SystemExit(0)

    if "--test" in sys.argv:
        for name, sc in SAMPLES.items():
            f = check(sc)
            print(f"\n{name}\n  {summarize(f)}")
            for x in f:
                print(f"    beat {x['beat']} {x['field']:9s} [{x['rule']}] -> {x['match']!r}")
        raise SystemExit(0)

    if len(sys.argv) < 2:
        sys.exit("usage: style_guard.py [--list | --test | script.json]")

    import json
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sc = data.get("script", data)
    found = check(sc)
    print(summarize(found))
    if found:
        print()
        print(revision_note(found))
        raise SystemExit(1)
