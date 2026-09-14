#!/usr/bin/env python3
"""
HackReel backend.

Runs standalone on a server. No Claude Code, no terminal, no operator: a
request arrives with a text prompt and the service does the rest. The only
credential it needs is GEMINI_API_KEY in the environment.

    pip install google-genai fastapi uvicorn
    uvicorn service.main:app --reload --port 8000   # from the hackreel/ root

Then open http://127.0.0.1:8000/

Endpoint status:
    GET  /api/health    live
    POST /api/generate  live  - prompt -> script, or a follow-up question
    POST /api/render    501   - needs voice.py / slides.py / assemble.py
    POST /api/publish   501   - needs publish.py AND the ntfy approval gate
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assemble  # noqa: E402
import script_gen  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
OUTPUT = ROOT / "assets" / "output"
TEMP = ROOT / "assets" / "temp"
for d in (OUTPUT, TEMP):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="HackReel", version="0.1.0")


def _output_url(path: str) -> str:
    """Map a bundle file on disk to its /output URL. Returns '' for anything
    outside assets/output rather than leaking an arbitrary filesystem path."""
    try:
        return "/output/" + Path(path).resolve().relative_to(OUTPUT).as_posix()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    prompt: str = Field(..., max_length=8000)
    effort: str = Field("high", pattern="^(low|medium|high|xhigh|max)$")


class RenderRequest(BaseModel):
    script: dict
    format: str = Field("auto", pattern="^(auto|video|carousel)$")


class PublishRequest(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "model": script_gen.MODEL,
        "provider": "google-gemini",
        # Whether a key is present, never the key itself.
        "api_key_present": script_gen.has_api_key(),
        "render_available": True,
        "render_formats": (["carousel", "video"] if assemble.ffmpeg_version()
                           else ["carousel"]),
        "ffmpeg": assemble.ffmpeg_version() or "not installed",
        "publish_available": False,
    }


@app.post("/api/generate")
def generate(req: GenerateRequest) -> JSONResponse:
    """Prompt in; a validated script, or a specific follow-up question, out.

    Synchronous: two Opus calls at high effort, so expect tens of seconds. The
    browser holds the connection. When render lands (which is far slower) this
    should move to a job id + polling.
    """
    result = script_gen.generate(req.prompt, effort=req.effort)
    code = 200
    if result.status == "error":
        # 503 when retrying could plausibly work, 400 when it never will.
        code = 503 if result.retryable else 400
    return JSONResponse(status_code=code, content=result.to_dict())


@app.post("/api/render")
def render(req: RenderRequest) -> JSONResponse:
    """Script -> a finished bundle on disk.

    Carousel works today. Video returns 501 until ffmpeg is available; the
    error says so explicitly rather than failing somewhere deep in assembly.
    """
    if not (req.script or {}).get("beats"):
        return JSONResponse(status_code=400,
                            content={"status": "error", "retryable": False,
                                     "error": "no script supplied"})
    try:
        post = assemble.assemble(req.script, OUTPUT, req.format)
    except assemble.AssemblyError as e:
        # The video branch is a missing capability, not a bad request.
        code = 501 if "video path is not implemented" in str(e) else 400
        return JSONResponse(status_code=code,
                            content={"status": "error", "error": str(e), "retryable": False})
    except Exception as e:  # noqa: BLE001 - surface anything else as a real failure
        return JSONResponse(status_code=500,
                            content={"status": "error", "retryable": False,
                                     "error": f"render failed: {e}"})

    # Rewrite on-disk paths into URLs the browser can actually load. A video
    # bundle has no "images" key at all, so key off the format rather than
    # assuming the carousel shape.
    post = dict(post)
    if post.get("images"):
        post["images"] = [{**im, "url": _output_url(im["path"])} for im in post["images"]]
    if post.get("video"):
        post["video_url"] = _output_url(post["video"])
    post["contact_sheet_url"] = _output_url(post.get("contact_sheet", ""))
    post["status"] = "ok"
    return JSONResponse(content=post)


@app.post("/api/publish")
def publish(req: PublishRequest) -> JSONResponse:
    """Not implemented, and deliberately fails closed.

    Do not implement this as a plain Zernio call. The rules carried over from
    MLTok Agent are non-negotiable and must ALL hold before anything leaves
    the account:

      1. Phone approval first. Push the pending post to the ntfy topic in
         ~/.claude/mltok-ntfy.conf and block until a reply of exactly "yes".
         Anything else, and any timeout, denies. Fail closed on every error.
      2. Only accept replies published AFTER the request went out (`since=`),
         or a stale approval gets replayed.
      3. tiktokSettings.draft: true AND publishNow: true, privacy SELF_ONLY,
         unless the user has explicitly approved a real publish for this post.
      4. The API key is read from the environment. It is never written to a
         file, a log line, or the ntfy message body.

    A publish path that returns success without (1) is worse than no publish
    path at all, so this stays 501 until the gate is wired and tested.
    """
    return JSONResponse(
        status_code=501,
        content={"status": "error",
                 "error": "publish is not implemented yet - it stays disabled until the "
                          "ntfy phone-approval gate is wired and tested",
                 "retryable": False},
    )


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------

if OUTPUT.exists():
    app.mount("/output", StaticFiles(directory=str(OUTPUT)), name="output")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(WEB / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
