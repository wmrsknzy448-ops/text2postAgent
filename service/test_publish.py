#!/usr/bin/env python3
"""
Tests for publish.py - the only module that can make something leave the
account. Everything here runs offline: no Zernio, no ntfy, no network.

    python test_publish.py

Every network boundary is injected, so the approval gate is exercised against
the failure modes that actually matter - stale replays, wrong nonces,
substring near-misses, silence, and an unreachable channel. The gate must deny
in every one of them.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


def expect_refusal(name: str, fn, must_mention: str = "") -> None:
    try:
        fn()
        check(name, False, "did NOT refuse")
    except publish.PublishError as e:
        check(name, must_mention.lower() in str(e).lower(),
              f"refused but message lacked {must_mention!r}: {e}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception type: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------

def make_bundle(tmp: Path, **overrides) -> Path:
    d = tmp / "bundle"
    d.mkdir(parents=True, exist_ok=True)
    imgs = []
    for i in range(3):
        p = d / f"slide_{i:02d}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        imgs.append({"index": i, "path": str(p), "bytes": p.stat().st_size,
                     "width": 1080, "height": 1920, "role": "hook",
                     "headline": f"HEADLINE {i}"})
    post = {
        "schema": "hackreel/post@1", "format": "carousel", "media_type": "photo",
        "project": "TestProject", "cover_index": 0, "images": imgs,
        "contact_sheet": str(d / "contact_sheet.png"),
        "caption": "a caption", "hashtags": ["#x"], "caption_reaches_app": False,
        "privacy": "SELF_ONLY", "draft": True, "approved_for_real_publish": False,
    }
    post.update(overrides)
    (d / "post.json").write_text(json.dumps(post), encoding="utf-8")
    return d


CFG = {"server": "https://ntfy.example", "topic": "req", "reply_topic": "rep"}


def fake_poller(messages):
    """A poller that ignores `since` - so tests that rely on since-filtering
    must do that filtering themselves, exactly as ntfy would."""
    return lambda cfg, since: messages


# ---------------------------------------------------------------------------

def test_safety_assertions():
    print("\nsafety assertions")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        ok = make_bundle(tmp)
        publish.load_bundle(ok)
        check("valid bundle loads", True)

        expect_refusal("public privacy refused",
                       lambda: publish.assert_safe(
                           {"privacy": "PUBLIC_TO_EVERYONE", "draft": True}, False),
                       "SELF_ONLY")
        expect_refusal("draft=false refused",
                       lambda: publish.assert_safe(
                           {"privacy": "SELF_ONLY", "draft": False}, False),
                       "draft")
        expect_refusal("--real-publish without bundle consent refused",
                       lambda: publish.assert_safe(
                           {"privacy": "SELF_ONLY", "draft": True,
                            "approved_for_real_publish": False}, True),
                       "cannot escalate")

        bad = make_bundle(tmp, schema="something/else@9")
        expect_refusal("wrong schema refused", lambda: publish.load_bundle(bad), "schema")

        d = make_bundle(tmp)
        p = json.loads((d / "post.json").read_text())
        p["images"][1]["path"] = str(tmp / "gone.png")
        (d / "post.json").write_text(json.dumps(p))
        expect_refusal("missing image file refused", lambda: publish.load_bundle(d), "missing")

        empty = make_bundle(tmp)
        p = json.loads((empty / "post.json").read_text()); p["images"] = []
        (empty / "post.json").write_text(json.dumps(p))
        expect_refusal("no media refused", lambda: publish.load_bundle(empty), "no media")


def test_payload():
    print("\npayload construction")
    post = {"caption": "hello", "images": [], "project": "P"}
    pl = publish.build_payload(post, "ACC1", ["u1", "u2"])
    tt = pl["platforms"][0]["platformSpecificData"]["tiktokSettings"]

    check("publishNow true", pl.get("publishNow") is True,
          "without publishNow the post never leaves Zernio")
    check("draft true", tt.get("draft") is True)
    check("privacy SELF_ONLY", tt.get("privacy_level") == "SELF_ONLY")
    check("media_type photo", tt.get("media_type") == "photo")
    check("platform tiktok", pl["platforms"][0]["platform"] == "tiktok")
    check("account id carried", pl["platforms"][0]["accountId"] == "ACC1")
    check("media as typed mediaItems in order",
          pl["mediaItems"] == [{"type": "image", "url": "u1"},
                               {"type": "image", "url": "u2"}],
          str(pl.get("mediaItems")))
    check("no legacy flat mediaUrls", "mediaUrls" not in pl, str(list(pl)))
    check("allow_comment present", tt.get("allow_comment") is False, str(tt))
    check("content_preview_confirmed true", tt.get("content_preview_confirmed") is True, str(tt))
    check("express_consent_given true", tt.get("express_consent_given") is True, str(tt))
    check("photo_cover_index carried", tt.get("photo_cover_index") == 0, str(tt))

    # Placement guard. docs.zernio.com says a tiktokSettings nested inside
    # platformSpecificData "is not recognized"; this module's history says a
    # nested one WAS read. Until a live run settles it, both must be sent -
    # because if the settings are dropped, draft and privacy_level go with
    # them while publishNow still applies, turning a SELF_ONLY draft into a
    # public post. These assertions exist so nobody "tidies up" the duplicate.
    top = pl.get("tiktokSettings")
    check("tiktokSettings present at TOP LEVEL", isinstance(top, dict),
          "docs say the nested copy is ignored; without this, privacy_level "
          "and draft are silently dropped and publishNow publishes publicly")
    check("nested tiktokSettings also present", isinstance(tt, dict))
    check("both copies identical", top == tt,
          "divergent copies mean the effective settings depend on which Zernio reads")
    check("top-level copy is SELF_ONLY + draft",
          top.get("privacy_level") == "SELF_ONLY" and top.get("draft") is True)
    check("media is mediaItems, not mediaUrls",
          "mediaUrls" not in pl and isinstance(pl.get("mediaItems"), list),
          "a flat mediaUrls list reads as no media at all")
    for req in ("privacy_level", "allow_comment", "content_preview_confirmed",
                "express_consent_given"):
        check(f"required field present: {req}", req in top)

    expect_refusal("no account id refused",
                   lambda: publish.build_payload(post, "", ["u"]), "account")
    expect_refusal("no media refused",
                   lambda: publish.build_payload(post, "ACC", []), "media")

    real = publish.build_payload(post, "ACC", ["u"], real_publish=True)
    rtt = real["platforms"][0]["platformSpecificData"]["tiktokSettings"]
    check("real publish flips draft off", rtt["draft"] is False)
    check("real publish is public", rtt["privacy_level"] == "PUBLIC_TO_EVERYONE")


def test_gate():
    print("\napproval gate (must fail closed)")
    N = "abcd1234"
    msg = lambda text: {"event": "message", "message": text}

    check("exact approval accepted",
          publish.await_approval(N, CFG, poller=fake_poller([msg(f"APPROVE {N}")])) is True)
    check("lowercase approval accepted",
          publish.await_approval(N, CFG, poller=fake_poller([msg(f"approve {N}")])) is True)
    check("whitespace tolerated",
          publish.await_approval(N, CFG, poller=fake_poller([msg(f"  APPROVE {N}  ")])) is True)

    check("explicit rejection denies",
          publish.await_approval(N, CFG, poller=fake_poller([msg(f"REJECT {N}")])) is False)
    check("wrong nonce denies",
          publish.await_approval(N, CFG, timeout=0,
                                 poller=fake_poller([msg("APPROVE 99999999")])) is False)
    check("substring near-miss denies",
          publish.await_approval(N, CFG, timeout=0,
                                 poller=fake_poller([msg(f"please APPROVE {N} later")])) is False,
          "a caption containing the token must not self-approve")
    check("unrelated chatter denies",
          publish.await_approval(N, CFG, timeout=0,
                                 poller=fake_poller([msg("yes"), msg("ok"), msg("sure")])) is False)
    check("non-message events ignored",
          publish.await_approval(N, CFG, timeout=0, poller=fake_poller(
              [{"event": "open"}, {"event": "keepalive", "message": f"APPROVE {N}"}])) is False)

    # Silence must never read as consent.
    ticks = {"t": 0.0}
    def clock():
        ticks["t"] += 30
        return ticks["t"]
    check("timeout denies",
          publish.await_approval(N, CFG, timeout=60, poller=fake_poller([]),
                                 sleep=lambda s: None, now=clock) is False)

    def boom(cfg, since):
        raise OSError("ntfy unreachable")
    check("channel error denies",
          publish.await_approval(N, CFG, poller=boom) is False)

    # A reply that predates the request must not be replayed. The real ntfy
    # applies `since` server-side; assert we pass the right value through.
    seen = {}
    def recording(cfg, since):
        seen["since"] = since
        return []
    publish.await_approval(N, CFG, timeout=0, since=1700000000, poller=recording,
                           sleep=lambda s: None)
    check("since is forwarded to the poller", seen.get("since") == 1700000000,
          "without since=, a stale approval replays")


def test_redaction():
    print("\nsecret redaction")
    key = "sk_" + "a" * 64
    out = publish.redact(f"failed with {key} and Authorization: Bearer {key}")
    check("api key not leaked", key not in out, out)
    check("bearer token not leaked", "Bearer " + key not in out, out)
    check("something still readable", "failed with" in out)


def test_dry_run_touches_nothing():
    print("\ndry run")
    with tempfile.TemporaryDirectory() as t:
        d = make_bundle(Path(t))
        calls = []
        def spy(*a, **k):
            calls.append(a)
            raise AssertionError("dry run must not make network calls")
        out = publish.publish(d, send=False, account_id="ACC", http=spy)
        check("no network calls in dry run", not calls)
        check("status is dry-run", out["status"] == "dry-run")
        check("payload still built", out["payload"]["publishNow"] is True)
        check("approval preview lists headlines",
              "HEADLINE 0" in out["approval_message"])
        check("approval preview states SELF_ONLY",
              "SELF_ONLY" in out["approval_message"])


def test_denied_publishes_nothing():
    print("\ndenial path")
    with tempfile.TemporaryDirectory() as t:
        d = make_bundle(Path(t))
        seen = []
        # Force fake topics. Without this the real ~/.claude/mltok-ntfy.conf is
        # picked up and the test aims at the operator's actual phone topic.
        env_saved = {k: publish.os.environ.get(k)
                     for k in ("ZERNIO_API_KEY", "NTFY_SERVER", "NTFY_TOPIC",
                               "NTFY_REPLY_TOPIC")}
        publish.os.environ["NTFY_SERVER"] = CFG["server"]
        publish.os.environ["NTFY_TOPIC"] = CFG["topic"]
        publish.os.environ["NTFY_REPLY_TOPIC"] = CFG["reply_topic"]

        def http(url, *, method="GET", data=None, headers=None, timeout=60):
            seen.append((method, url))
            if "media/presign" in url:
                return 200, json.dumps({"uploadUrl": "https://up.example/x",
                                        "publicUrl": "https://cdn.example/x.png"}).encode()
            if url.startswith("https://up.example"):
                return 200, b""
            if f"/{CFG['reply_topic']}/json" in url:
                return 200, b""              # silence from the phone
            if url.endswith(f"/{CFG['topic']}"):
                return 200, b""              # approval request accepted
            raise AssertionError(f"unexpected call: {method} {url}")

        publish.os.environ["ZERNIO_API_KEY"] = "sk_" + "b" * 64
        try:
            out = publish.publish(d, send=True, account_id="ACC", timeout=0, http=http)
        finally:
            for k, v in env_saved.items():
                if v is None:
                    publish.os.environ.pop(k, None)
                else:
                    publish.os.environ[k] = v

        check("status denied", out["status"] == "denied", str(out))
        check("createPost never called",
              not any(u.endswith("/posts") for _, u in seen),
              f"calls made: {seen}")


def test_missing_config_refuses():
    print("\nmissing configuration")
    saved = {k: publish.os.environ.get(k)
             for k in ("ZERNIO_API_KEY", "NTFY_TOPIC", "NTFY_REPLY_TOPIC",
                       "HACKREEL_NTFY_CONF")}
    try:
        publish.os.environ.pop("ZERNIO_API_KEY", None)
        expect_refusal("no zernio key refused", publish.zernio_key, "ZERNIO_API_KEY")
        # Point the conf lookup at nothing so the real machine's file can't
        # accidentally satisfy this test.
        publish.os.environ["HACKREEL_NTFY_CONF"] = str(Path(tempfile.gettempdir()) / "nope.conf")
        publish.os.environ.pop("NTFY_TOPIC", None)
        publish.os.environ.pop("NTFY_REPLY_TOPIC", None)
        expect_refusal("no ntfy topics refused", publish.ntfy_config, "approval channel")
    finally:
        for k, v in saved.items():
            if v is None:
                publish.os.environ.pop(k, None)
            else:
                publish.os.environ[k] = v


def test_presign_request_shape():
    """The presign body uses the API's field names, not ours.

    A live 400 ("expected string, received undefined", param: filename) got
    past 41 green tests because every mock discarded `data`. Assert on the
    bytes actually sent, or this whole suite stays blind to the request shape.
    """
    print("")
    print("presign request shape")
    bodies = []
    with tempfile.TemporaryDirectory() as t:
        d = Path(t) / "b"
        d.mkdir()
        img = d / "slide_00.png"
        img.write_bytes(b"0" * 64)

        def http(url, *, method="GET", data=None, headers=None, timeout=60):
            if "media/presign" in url:
                bodies.append(json.loads(data))
                return 200, json.dumps({"uploadUrl": "https://up.example/x",
                                        "publicUrl": "https://cdn.example/x.png"}).encode()
            return 200, b""

        publish.upload_media([img], "sk_" + "c" * 64, http=http)

    body = bodies[0] if bodies else {}
    check("presign sends 'filename'", body.get("filename") == "slide_00.png", str(body))
    check("presign does NOT send 'fileName'", "fileName" not in body, str(body))
    check("presign sends 'contentType'", body.get("contentType") == "image/png", str(body))
    check("presign sends 'size' as an int", isinstance(body.get("size"), int), str(body))
    check("presign does NOT send 'fileSize'", "fileSize" not in body, str(body))


if __name__ == "__main__":
    for fn in (test_safety_assertions, test_payload, test_gate, test_redaction,
               test_dry_run_touches_nothing, test_denied_publishes_nothing,
               test_missing_config_refuses, test_presign_request_shape):
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
    raise SystemExit(1 if FAIL else 0)
