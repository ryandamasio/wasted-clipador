"""App FastAPI da fila de aprovação. Interface única (templates/index.html) + API JSON.

  GET  /                         página (responsiva, celular)
  GET  /api/clips?status=ready   lista
  GET  /api/stats
  POST /api/clips/{id}/approve   {title, networks:{net:{title,description}}, note}
  POST /api/clips/{id}/reject    {reason}
  POST /api/clips/{id}/recut     {start_delta, end_delta}
  POST /api/clips/{id}/reedit
  POST /api/clips/{id}/restore
  POST /api/clips/{id}/posted    {platforms:[...]}
  GET  /media/{id}/{clip.mp4|clip_youtube.mp4|cover.jpg}
"""
from __future__ import annotations

import base64
import logging
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader

from clipador.config import Config
from clipador.review.service import REJECT_REASONS, ReviewError, ReviewService
from clipador.store.db import Database

log = logging.getLogger("review.app")
TEMPLATES = Path(__file__).parent / "templates"


def create_app(cfg: Config, db: Database, scheduler=None) -> FastAPI:
    svc = ReviewService(cfg, db, scheduler)
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    app = FastAPI(title="Clipador — fila de aprovação", docs_url=None, redoc_url=None)
    app.state.service = svc
    password = (os.environ.get("REVIEW_PASSWORD") or "").strip()

    @app.middleware("http")
    async def _basic_auth(request: Request, call_next):
        """Com REVIEW_PASSWORD no .env, exige usuário 'wasted' + senha (HTTP Basic: o navegador lembra)."""
        if password:
            ok = False
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("basic "):
                try:
                    user, _, pwd = base64.b64decode(auth[6:]).decode("utf-8", "replace").partition(":")
                    ok = secrets.compare_digest(pwd, password) and user in ("wasted", "", "admin", "ryan")
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response("senha necessária", status_code=401, headers={"WWW-Authenticate": 'Basic realm="WASTED"'})
        return await call_next(request)

    @app.exception_handler(ReviewError)
    async def _review_error(_req: Request, exc: ReviewError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def index():
        tpl = env.get_template("index.html")
        return tpl.render(profile=cfg.settings.get("copy.profile_name", "Clipador"), reject_reasons=REJECT_REASONS,
                          auto_min=cfg.settings.get("review.auto_approve_min_score"))

    @app.get("/api/clips")
    def api_clips(status: str = "ready", limit: int = 100):
        return {"clips": svc.list_clips(status, min(max(limit, 1), 500)), "stats": svc.stats()}

    @app.get("/api/clips/{clip_id}")
    def api_clip(clip_id: int):
        return svc.get(clip_id)

    @app.get("/api/stats")
    def api_stats():
        return svc.stats()

    @app.post("/api/clips/{clip_id}/approve")
    async def api_approve(clip_id: int, request: Request):
        body = await _json(request)
        return svc.approve(clip_id, body.get("title"), body.get("networks"), body.get("note"))

    @app.post("/api/clips/{clip_id}/reject")
    async def api_reject(clip_id: int, request: Request):
        body = await _json(request)
        return svc.reject(clip_id, body.get("reason"))

    @app.post("/api/clips/{clip_id}/recut")
    async def api_recut(clip_id: int, request: Request):
        body = await _json(request)
        return svc.recut(clip_id, float(body.get("start_delta") or 0), float(body.get("end_delta") or 0))

    @app.post("/api/clips/{clip_id}/reedit")
    def api_reedit(clip_id: int):
        return svc.recut(clip_id, 0.0, 0.0, reedit=True)

    @app.post("/api/clips/{clip_id}/restore")
    def api_restore(clip_id: int):
        return svc.restore(clip_id)

    @app.post("/api/clips/{clip_id}/posted")
    async def api_posted(clip_id: int, request: Request):
        body = await _json(request)
        return svc.mark_posted(clip_id, body.get("platforms"))

    @app.get("/api/metrics")
    def api_metrics(days: int = 7):
        return svc.metrics(min(max(days, 1), 90))

    @app.get("/api/posts")
    def api_posts(status: str = "scheduled,posting,failed,processing", limit: int = 200):
        return {"posts": svc.list_posts(status, min(max(limit, 1), 500)), "publish": svc.publish_status(), "stats": svc.stats()}

    @app.post("/api/posts/{post_id}/{action}")
    async def api_post_action(post_id: int, action: str, request: Request):
        body = await _json(request)
        return svc.post_action(post_id, action, body.get("url"))

    @app.get("/media/{clip_id}/{name}")
    def media(clip_id: int, name: str):
        if name not in ("clip.mp4", "clip_youtube.mp4", "clip_long.mp4", "cover.jpg"):
            raise HTTPException(404)
        try:
            path = svc.media_path(clip_id, name)
        except ReviewError as exc:
            raise HTTPException(404, str(exc)) from exc
        media_type = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
        return FileResponse(path, media_type=media_type, filename=f"clipador_{clip_id}_{name}" if name.endswith(".mp4") else None,
                            content_disposition_type="inline")

    return app


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


class ReviewServer:
    """uvicorn numa thread daemon; `stop()` encerra."""

    def __init__(self, cfg: Config, db: Database, scheduler=None):
        import uvicorn

        r = cfg.settings.section("review")
        self.host = str(r.get("host", "0.0.0.0"))
        self.port = int(r.get("port", 8787))
        self.app = create_app(cfg, db, scheduler)
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="review-web", daemon=True)
        self.tunnel = None
        if r.get("tunnel"):
            from clipador.review.tunnel import Tunnel

            if (os.environ.get("REVIEW_PASSWORD") or "").strip():
                self.tunnel = Tunnel(self.port, cfg.settings.path("data_dir") / "tunnel_url.txt")
            else:
                log.error("review.tunnel ligado mas sem REVIEW_PASSWORD no .env: túnel NÃO aberto (seria público sem senha)")

    def start(self) -> None:
        self.thread.start()
        log.info("fila de aprovação em http://%s:%d (no celular use o IP deste PC na rede local)",
                 "localhost" if self.host in ("0.0.0.0", "::") else self.host, self.port)
        if self.tunnel:
            self.tunnel.start()

    def stop(self) -> None:
        if self.tunnel:
            self.tunnel.stop()
        self.server.should_exit = True
        self.thread.join(timeout=5)
