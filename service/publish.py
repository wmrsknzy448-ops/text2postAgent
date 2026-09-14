#!/usr/bin/env python3
"""
HackReel - publish. Zernio -> TikTok draft, behind a phone-approval gate.

This is the only module that can cause something to leave the account, so it
is built to refuse by default and to fail closed on every uncertainty.

Rules carried from MLTok Agent. Do not relax any of them:

  1. Nothing is created on Zernio without a phone approval that arrives AFTER
     the request was sent. Timeout denies. Any error denies.
  2. SELF_ONLY + draft unless the operator explicitly approved a real publish,
     which needs BOTH the --real-publish flag AND an approval reply to a
     message that says so in plain language.
  3. The API key is read from the environment. It is never written to a file,
     a log, or an ntfy message body.

Ordering note: media is uploaded BEFORE approval. Presigning and PUTting bytes
publishes nothing, and gating prep steps is what caused alert fatigue in MLTok
Agent - a gate that cries wolf gets rubber-stamped. Uploading first also means
the approval message can carry the actual cover image, so the operator sees
what they are approving instead of a filename.

Usage:
  python publish.py BUNDLE_DIR                  # dry run: prints, sends nothing
  python publish.py BUNDLE_DIR --send           # upload + phone gate + draft
  python publish.py BUNDLE_DIR --test-gate      # exercise ONLY the ntfy gate
"""

from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

ZERNIO_BASE = os.environ.get("ZERNIO_BASE", "https://zernio.com/api/v1")

# Discovered the hard way, 2026-08-31, from a live 400 (TIKTOK_PHOTO_TITLE_TOO_LONG):
# for a PHOTO post TikTok reuses `content` as the slideshow TITLE and caps it at
# 90 characters. Not mentioned on any Zernio docs page consulted. A caption with
# hashtags blows straight past it - ours was 99.
PHOTO_TITLE_MAX = 90


def photo_title(caption: str) -> str:
    """A caption is not a title. Cut one out of the other.

    Hashtags are stripped rather than truncated into: they are worthless in a
    slideshow title and they are what pushes it over the cap. The full text,
    hashtags and all, still travels in tiktokSettings.description, which allows
    4000 characters.
    """
    text = re.sub(r"#\w+", "", caption or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= PHOTO_TITLE_MAX:
        return text
    cut = text[:PHOTO_TITLE_MAX]
    if " " in cut:                      # never sever a word
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-—–.")


def _title_for(post: dict) -> str:
    """Photo title, never empty. A caption that is only hashtags trims to
    nothing, and an empty title is a 400 of its own."""
    return photo_title(post.get("caption", "")) or (post.get("project") or "HackReel")


DEFAULT_TIMEOUT = 300          # seconds to wait for a phone reply
POLL_INTERVAL = 3

_SECRET_RE = re.compile(r"(sk_[A-Za-z0-9]{4})[A-Za-z0-9]+|(Bearer\s+)\S+")


class PublishError(Exception):
    """Refusal or failure. Never partially publish on this path."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _conf_file() -> dict:
    """Read the MLTok-style ntfy conf (topic names only, never keys).
    Env vars win, so a server deployment needs no file at all."""
    path = Path(os.environ.get(
        "HACKREEL_NTFY_CONF", str(Path.home() / ".claude" / "mltok-ntfy.conf")))
    out: dict = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ntfy_config() -> dict:
    conf = _conf_file()
    cfg = {
        "server": os.environ.get("NTFY_SERVER") or conf.get("NTFY_SERVER") or "https://ntfy.sh",
        "topic": os.environ.get("NTFY_TOPIC") or conf.get("NTFY_TOPIC") or "",
        "reply_topic": (os.environ.get("NTFY_REPLY_TOPIC")
                        or conf.get("NTFY_REPLY_TOPIC") or ""),
    }
    if not cfg["topic"] or not cfg["reply_topic"]:
        raise PublishError(
            "ntfy topics are not configured; set NTFY_TOPIC and NTFY_REPLY_TOPIC "
            "(or provide the conf file). Refusing to publish without an approval channel."
        )
    return cfg


def zernio_key() -> str:
    key = os.environ.get("ZERNIO_API_KEY", "").strip()
    if not key:
        raise PublishError(
            "ZERNIO_API_KEY is not set. It is read from the environment only - "
            "never hardcode it into a file."
        )
    return key


def redact(text: str) -> str:
    """Strip anything key-shaped before it can reach a phone or a log."""
    return _SECRET_RE.sub(lambda m: (m.group(1) + "...") if m.group(1) else "Bearer ...", text)


# ---------------------------------------------------------------------------
# Bundle loading + safety assertions
# ---------------------------------------------------------------------------

def load_bundle(bundle_dir: Path) -> dict:
    pj = Path(bundle_dir) / "post.json"
    if not pj.is_file():
        raise PublishError(f"no post.json in {bundle_dir}")
    post = json.loads(pj.read_text(encoding="utf-8"))
    if post.get("schema") != "hackreel/post@1":
        raise PublishError(f"unexpected post schema: {post.get('schema')!r}")
    # A carousel bundle carries "images"; a video bundle carries "video".
    # Requiring "images" unconditionally would refuse every video bundle.
    media = [im["path"] for im in (post.get("images") or [])]
    if post.get("video"):
        media.append(post["video"])
    if not media:
        raise PublishError("bundle has no media (neither images nor video)")
    for path in media:
        if not Path(path).is_file():
            raise PublishError(f"bundle references a missing file: {path}")
    return post


def assert_safe(post: dict, real_publish: bool) -> None:
    """The bundle declares its own intended blast radius; enforce it here.

    A real publish requires the flag AND the bundle's own consent, so a
    stale bundle can never be escalated by a flag alone.
    """
    if not real_publish:
        if post.get("privacy") != "SELF_ONLY":
            raise PublishError(
                f"bundle privacy is {post.get('privacy')!r}, expected SELF_ONLY. Refusing.")
        if post.get("draft") is not True:
            raise PublishError("bundle is not marked draft. Refusing.")
        return

    if not post.get("approved_for_real_publish"):
        raise PublishError(
            "--real-publish was passed but the bundle has "
            "approved_for_real_publish=false. Refusing: the flag alone cannot "
            "escalate a bundle that was not built for a real publish."
        )


def build_payload(post: dict, account_id: str, media_urls: list[str],
                  real_publish: bool = False) -> dict:
    """Pure function: bundle + uploaded URLs -> the exact Zernio request body.

    Draft mode is `tiktokSettings.draft: true` AND `publishNow: true`. Without
    publishNow the post never leaves Zernio; `draft` redirects the delivery to
    the TikTok Creator Inbox rather than the public feed. Both are required.

    TikTok's own docs say draft and publishNow "serve opposite purposes" and
    should not be combined. They are combined here deliberately, and it is
    verified, not assumed: a live run returned platformPostId
    "p_inbox_url~v2...", and that p_inbox_url prefix is TikTok saying it routed
    the post to the Creator Inbox rather than the feed, with isDraft true and
    privacy_level SELF_ONLY echoed back unchanged. Keep both fields.
    """
    if not account_id:
        raise PublishError("no TikTok account id (set ZERNIO_ACCOUNT_ID)")
    if not media_urls:
        raise PublishError("no uploaded media urls")

    # Field names and required-ness are TikTok's, via Zernio. For a photo post
    # privacy_level, allow_comment, media_type, content_preview_confirmed and
    # express_consent_given are all mandatory - omitting the last two is a 400.
    #
    # The two consent flags are attestations that a human previewed this exact
    # content and consented to it being posted. That is only true here because
    # build_payload runs AFTER the phone gate returned an approval, with the
    # contact sheet attached to the request. Do not hoist this call above the
    # gate: it would turn the flags into a lie.
    tiktok: dict = {
        # Follow the bundle. A video bundle announced as a photo carousel is
        # a malformed post, and assemble.py already recorded which it is.
        "media_type": post.get("media_type", "photo"),
        "privacy_level": "SELF_ONLY" if not real_publish else "PUBLIC_TO_EVERYONE",
        "draft": not real_publish,
        "allow_comment": False,
        "photo_cover_index": post.get("cover_index", 0),
        "content_preview_confirmed": True,
        "express_consent_given": True,
        # The full caption, hashtags included, up to 4000 chars. `content` is
        # only the 90-char slideshow title for photo posts, so without this the
        # hashtags would simply be discarded.
        "description": post.get("caption", ""),
    }
    caption = post.get("caption", "")
    return {
        # For a photo post this becomes the slideshow TITLE (90 char cap), not
        # a caption. The full text lives in tiktokSettings.description above.
        # In draft mode neither reaches the TikTok app anyway - the readable
        # content is rendered into the slides - but the API still validates it.
        "content": (_title_for(post)
                    if tiktok["media_type"] == "photo" else caption),
        # NOT `mediaUrls`, and not a flat list of strings: Zernio wants typed
        # media objects at the top level. A flat list is silently seen as no
        # media at all -> "Tiktok posts require media content".
        "mediaItems": [
            {"type": "video" if post.get("media_type") == "video" else "image",
             "url": u} for u in media_urls],
        "publishNow": True,
        # Sent in BOTH placements, deliberately, with identical content.
        #
        # docs.zernio.com/guides/platform-settings (checked 2026-08-28) is
        # explicit: "TikTok settings go in a tiktokSettings object at the top
        # level of the request body (a tiktokSettings key nested inside
        # platformSpecificData is not recognized)." The create-post schema,
        # however, shows the key in both places, and this module's own history
        # records a run that landed in the Creator Inbox while nesting it -
        # which could only happen if the nested copy WAS read.
        #
        # The two claims cannot both be true, and getting it wrong in the
        # top-level direction is the dangerous one: if the settings are
        # dropped, draft and privacy_level go with them while publishNow still
        # applies, and a SELF_ONLY draft becomes a public post. Duplicating
        # costs nothing (the objects are identical, so there is no conflict to
        # resolve) and removes the failure mode entirely.
        #
        # Collapse this to one placement only after a live run proves which
        # copy is read - check that the response echoes privacy_level SELF_ONLY
        # and returns a p_inbox_url~ platformPostId.
        "tiktokSettings": tiktok,
        "platforms": [{
            "platform": "tiktok",
            "accountId": account_id,
            "platformSpecificData": {"tiktokSettings": tiktok},
        }],
    }


# ---------------------------------------------------------------------------
# HTTP (thin, stdlib only, injectable for tests)
# ---------------------------------------------------------------------------

def _http(url: str, *, method: str = "GET", data: bytes | None = None,
          headers: dict | None = None, timeout: int = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upload_media(paths: list[Path], key: str, http: Callable = _http) -> list[str]:
    """presign -> PUT bytes -> publicUrl, for each image, in order."""
    urls = []
    auth = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for p in paths:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        # Field names are the API's, not ours: `filename` (lowercase) and
        # `size`. `fileName`/`fileSize` return a 400 that names only the first
        # offender, so both were wrong at once.
        body = json.dumps({"filename": p.name, "contentType": ctype,
                           "size": p.stat().st_size}).encode()
        code, raw = http(f"{ZERNIO_BASE}/media/presign", method="POST",
                         data=body, headers=auth)
        if code >= 300:
            raise PublishError(f"presign failed ({code}): {redact(raw.decode('utf-8', 'replace'))[:300]}")
        info = json.loads(raw)
        up, pub = info.get("uploadUrl"), info.get("publicUrl")
        if not up or not pub:
            raise PublishError(f"presign response missing uploadUrl/publicUrl: {list(info)}")

        code, raw = http(up, method="PUT", data=p.read_bytes(),
                         headers={"Content-Type": ctype})
        if code >= 300:
            raise PublishError(f"upload of {p.name} failed ({code})")
        urls.append(pub)
    return urls


def create_post(payload: dict, key: str, http: Callable = _http) -> dict:
    code, raw = http(f"{ZERNIO_BASE}/posts", method="POST",
                     data=json.dumps(payload).encode(),
                     headers={"Authorization": f"Bearer {key}",
                              "Content-Type": "application/json"})
    text = raw.decode("utf-8", "replace")
    if code >= 300:
        raise PublishError(f"createPost failed ({code}): {redact(text)[:400]}")
    return json.loads(text)


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------

def media_paths(post: dict) -> list[Path]:
    """The files to upload, in order. A carousel has images; a video has one
    mp4. Reading post["images"] unconditionally crashes on a video bundle."""
    if post.get("video"):
        return [Path(post["video"])]
    return [Path(im["path"]) for im in (post.get("images") or [])]


def summarize(post: dict, real_publish: bool) -> str:
    is_video = bool(post.get("video"))
    lines = [f"HackReel: {post.get('project') or 'untitled'}"]

    if is_video:
        lines.append(f"video, {post.get('duration_sec', '?')}s, "
                     f"{len(post.get('beats', []))} slides")
        lines.append("")
        for b in post.get("beats", []):
            lines.append(f"  {b['index'] + 1}. {b.get('headline', '')}"
                         f"  ({b.get('duration_sec', 0):.1f}s)")
    else:
        lines.append(f"{len(post.get('images', []))} image carousel")
        lines.append("")
        for im in post.get("images", []):
            lines.append(f"  {im['index'] + 1}. {im.get('headline', '')}")
    lines.append("")
    if real_publish:
        lines.append("*** REAL PUBLIC PUBLISH - this goes live on TikTok ***")
    else:
        lines.append("SELF_ONLY + draft (lands in your TikTok Creator Inbox)")
    return redact("\n".join(lines))


def send_request(summary: str, nonce: str, cfg: dict, *,
                 attach_url: str = "", http: Callable = _http) -> None:
    headers = {
        "Title": "HackReel needs approval",
        "Priority": "high",
        "Tags": "warning,inbox_tray",
        "Actions": (
            f"http, Approve, {cfg['server']}/{cfg['reply_topic']}, method=POST, "
            f"body='APPROVE {nonce}', clear=true; "
            f"http, Reject, {cfg['server']}/{cfg['reply_topic']}, method=POST, "
            f"body='REJECT {nonce}', clear=true"
        ),
    }
    if attach_url:
        headers["Attach"] = attach_url
    body = f"{summary}\n\n[request id: {nonce}]".encode()
    code, _ = http(f"{cfg['server']}/{cfg['topic']}", method="POST",
                   data=body, headers=headers)
    if code >= 300:
        raise PublishError(f"could not send the approval request ({code}) - refusing to publish")


def poll_replies(cfg: dict, since: int, http: Callable = _http) -> list[dict]:
    url = f"{cfg['server']}/{cfg['reply_topic']}/json?poll=1&since={since}"
    code, raw = http(url, method="GET")
    if code >= 300:
        raise PublishError(f"could not read approval replies ({code})")
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # ignore anything unparseable rather than guess
    return out


def await_approval(nonce: str, cfg: dict, *, timeout: int = DEFAULT_TIMEOUT,
                   since: int | None = None, poller: Callable = poll_replies,
                   sleep: Callable = time.sleep, now: Callable = time.time) -> bool:
    """Block until the phone replies. Returns True ONLY on an exact approval.

    Fails closed: rejection, timeout, or any error is a denial. `since` is the
    moment the request went out, so an approval sent *before* it can never be
    replayed - that is a real incident from MLTok Agent, not a hypothetical.
    """
    approve, reject = f"APPROVE {nonce}".upper(), f"REJECT {nonce}".upper()
    started = now()
    since = int(started) if since is None else since

    while True:
        try:
            messages = poller(cfg, since)
        except Exception:
            return False                      # unreachable channel => denied
        for m in messages:
            if m.get("event") != "message":
                continue
            # Must EQUAL the token. A message merely containing it does not
            # approve anything - otherwise a caption could self-approve.
            text = (m.get("message") or "").strip().upper()
            if text == approve:
                return True
            if text == reject:
                return False
        if now() - started >= timeout:
            return False                      # silence is not consent
        sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def publish(bundle_dir: Path, *, send: bool = False, real_publish: bool = False,
            timeout: int = DEFAULT_TIMEOUT, account_id: str = "",
            http: Callable = _http) -> dict:
    post = load_bundle(bundle_dir)
    assert_safe(post, real_publish)
    account_id = account_id or os.environ.get("ZERNIO_ACCOUNT_ID", "")
    paths = media_paths(post)

    if not send:
        # Dry run: construct exactly what would be sent, contact nothing.
        payload = build_payload(post, account_id or "<ZERNIO_ACCOUNT_ID>",
                                [f"<uploaded:{p.name}>" for p in paths], real_publish)
        return {"status": "dry-run", "payload": payload,
                "approval_message": summarize(post, real_publish)}

    key = zernio_key()
    cfg = ntfy_config()

    media_urls = upload_media(paths, key, http=http)      # prep, publishes nothing

    nonce = secrets.token_hex(4)
    sent_at = int(time.time())
    cover = media_urls[post.get("cover_index", 0)] if media_urls else ""
    send_request(summarize(post, real_publish), nonce, cfg, attach_url=cover, http=http)

    if not await_approval(nonce, cfg, timeout=timeout, since=sent_at,
                          poller=lambda c, s: poll_replies(c, s, http=http)):
        return {"status": "denied", "nonce": nonce,
                "detail": "no approval (rejected, timed out, or channel error). Nothing published."}

    payload = build_payload(post, account_id, media_urls, real_publish)
    result = create_post(payload, key, http=http)

    # Write the receipt into the bundle. It used to be printed and nothing
    # else, so the one piece of evidence that a post actually landed - the
    # platformPostId - vanished with the terminal scrollback, and afterwards
    # nobody could tell a verified run from an assumed one.
    receipt = {
        "status": "published",
        "nonce": nonce,
        "sent_at": _dt.datetime.fromtimestamp(sent_at).astimezone().isoformat(
            timespec="seconds"),
        "real_publish": real_publish,
        "payload": payload,
        "zernio": result,
    }
    try:
        (Path(bundle_dir) / "publish_receipt.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass          # a post that succeeded must not be reported as failed
    return {"status": "published", "nonce": nonce, "zernio": result, "payload": payload,
            "receipt": str(Path(bundle_dir) / "publish_receipt.json")}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="HackReel publish (dry run by default)")
    ap.add_argument("bundle", help="a bundle directory containing post.json")
    ap.add_argument("--send", action="store_true", help="actually upload and publish")
    ap.add_argument("--real-publish", action="store_true",
                    help="PUBLIC publish instead of SELF_ONLY draft (needs bundle consent)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--account", default="",
                    help="TikTok account id; overrides ZERNIO_ACCOUNT_ID. Needed when "
                         "the env var was set after this shell started (setx does not "
                         "reach an already-running process).")
    ap.add_argument("--test-gate", action="store_true",
                    help="exercise only the ntfy approval round trip; touches no Zernio API")
    a = ap.parse_args()

    try:
        if a.test_gate:
            cfg = ntfy_config()
            nonce = secrets.token_hex(4)
            sent_at = int(time.time())
            send_request("HackReel gate test. Approving sends nothing anywhere.",
                         nonce, cfg)
            print(f"sent request {nonce}; waiting up to {a.timeout}s for your phone…")
            ok = await_approval(nonce, cfg, timeout=a.timeout, since=sent_at)
            print("APPROVED" if ok else "DENIED (rejected, timed out, or error)")
            raise SystemExit(0 if ok else 1)

        out = publish(Path(a.bundle), send=a.send, real_publish=a.real_publish,
                      timeout=a.timeout, account_id=a.account)
    except PublishError as e:
        raise SystemExit(f"publish refused: {e}")

    print(json.dumps(out, indent=2, ensure_ascii=False))
    if out["status"] == "dry-run":
        print("\nDRY RUN - nothing was uploaded, sent, or published. Add --send to do it.")
    raise SystemExit(0 if out["status"] in ("dry-run", "published") else 2)
