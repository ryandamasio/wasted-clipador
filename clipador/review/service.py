"""Regras da fila de aprovação (sem HTTP): aprovar, rejeitar, ajustar corte, re-editar, restaurar.

Cada decisão vira uma linha em `reviews` com a nota e os componentes da detecção, para calibrar depois
(`clipador status` mostra a taxa de aprovação por faixa de nota).
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

from clipador.config import Config
from clipador.copy.generator import write_post_files
from clipador.store.db import Database

log = logging.getLogger("review")

MIN_CLIP, MAX_CLIP = 5.0, 60.0
REJECT_REASONS = ["sem graça", "sem contexto", "corte ruim", "tema sensível", "repetido", "qualidade", "outro"]


class ReviewError(Exception):
    pass


class ReviewService:
    def __init__(self, cfg: Config, db: Database, scheduler=None):
        self.cfg = cfg
        self.db = db
        self._scheduler = scheduler

    @property
    def scheduler(self):
        if self._scheduler is None:
            from clipador.publish.scheduler import Scheduler

            self._scheduler = Scheduler(self.cfg, self.db)
        return self._scheduler

    # ------------------------------------------------------------------ leitura
    def list_clips(self, status: str = "ready", limit: int = 100) -> list[dict]:
        rows = self.db.clips_for_review(status, limit)
        return [self._enrich(r) for r in rows]

    def get(self, clip_id: int) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError(f"clipe {clip_id} não existe")
        rows = self.db.fetchall(
            """SELECT c.*, m.score, m.reasons, m.transcript, m.start_ts, m.end_ts, m.peak_ts, m.stream_id,
                      m.status AS moment_status, m.recut_count, s.streamer, s.platform, s.title AS stream_title
               FROM clips c JOIN moments m ON m.id = c.moment_id JOIN streams s ON s.id = m.stream_id WHERE c.id = ?""", [clip_id])
        return self._enrich(rows[0])

    def _enrich(self, r: dict) -> dict:
        reasons = json.loads(r["reasons"]) if isinstance(r.get("reasons"), str) else (r.get("reasons") or {})
        copy_data = json.loads(r["copy_json"]) if isinstance(r.get("copy_json"), str) else (r.get("copy_json") or {})
        render = json.loads(r["render_json"]) if r.get("render_json") else {}
        s = self.cfg.streamer(r["streamer"])
        clip_dir = Path(r["path"]).parent
        return {
            "id": r["id"], "moment_id": r["moment_id"], "status": r["status"], "score": round(float(r["score"] or 0)),
            "streamer": r["streamer"], "streamer_name": s.name if s else r["streamer"], "platform": r["platform"],
            "stream_title": r.get("stream_title"), "duration": round(float(r["duration"] or 0), 1),
            "start_ts": r["start_ts"], "end_ts": r["end_ts"], "when": time.strftime("%d/%m %H:%M", time.localtime(r["start_ts"])),
            "notes": reasons.get("notes", []), "components": {k: reasons.get(k) for k in ("chat", "audio", "transcript", "keywords")},
            "keywords": reasons.get("keywords_found", []), "transcript": r.get("transcript") or "",
            "titles": copy_data.get("titles", []), "chosen_title": r.get("chosen_title") or (copy_data.get("titles") or [""])[0],
            "networks": copy_data.get("networks", {}), "music": copy_data.get("music"), "music_note": copy_data.get("music_note"),
            "has_music_video": bool(r.get("music_path")) and Path(r["music_path"]).exists(),
            "has_long_video": bool(r.get("long_path")) and Path(r["long_path"]).exists(),
            "long_seconds": render.get("long_seconds"),
            "has_cover": bool(r.get("cover_path")) and Path(r["cover_path"]).exists(),
            "dir": str(clip_dir), "review_note": r.get("review_note"), "recut_count": int(r.get("recut_count") or 0),
            "media_available": self._media_available(r), "render": render,
            "created_at": r["created_at"], "reviewed_at": r.get("reviewed_at"),
        }

    def _media_available(self, r: dict) -> bool:
        segs = self.db.segments_covering(r["stream_id"], r["start_ts"] - 10, r["end_ts"] + 10)
        return any(Path(s["path"]).exists() for s in segs)

    def media_path(self, clip_id: int, name: str) -> Path:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        allowed = {"clip.mp4": clip["path"], "cover.jpg": clip.get("cover_path"), "clip_youtube.mp4": clip.get("music_path"),
                   "clip_long.mp4": clip.get("long_path")}
        p = allowed.get(name)
        if not p or not Path(p).exists():
            raise ReviewError(f"{name} indisponível")
        return Path(p)

    # ------------------------------------------------------------------ decisões
    def approve(self, clip_id: int, title: str | None = None, networks: dict | None = None, note: str | None = None) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        moment = self.db.get_moment(clip["moment_id"])
        copy_data = json.loads(clip["copy_json"]) if clip.get("copy_json") else {}
        title = (title or "").strip() or (copy_data.get("titles") or [clip.get("title") or ""])[0]
        if title and title not in copy_data.get("titles", []):
            copy_data.setdefault("titles", []).insert(0, title)
        for net, data in (networks or {}).items():
            if net in copy_data.get("networks", {}) and isinstance(data, dict):
                for key in ("title", "description"):
                    if isinstance(data.get(key), str) and data[key].strip():
                        copy_data["networks"][net][key] = data[key].strip()
        stream = self.db.get_stream(moment["stream_id"]) if moment else None
        streamer = self.cfg.streamer(stream["streamer"]) if stream else None
        out_dir = Path(clip["path"]).parent
        if streamer and moment and out_dir.exists():
            try:
                write_post_files(out_dir, copy_data, moment, streamer, out_dir.name, chosen_title=title)
            except Exception:  # noqa: BLE001
                log.exception("não consegui reescrever post.md do clipe %d", clip_id)
        self.db.update("clips", clip_id, status="approved", chosen_title=title, title=title,
                       copy_json=json.dumps(copy_data, ensure_ascii=False), review_note=note, reviewed_at=time.time())
        self.db.add_review(clip, "approved", note, chosen_title=title, moment=moment)
        log.info("clipe %d APROVADO: %s", clip_id, title)
        try:
            self.scheduler.schedule_clip(clip_id)
        except Exception:  # noqa: BLE001
            log.exception("agendamento do clipe %d falhou (aprovação mantida)", clip_id)
        return self.get(clip_id)

    def reject(self, clip_id: int, reason: str | None = None) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        self.db.set_clip_status(clip_id, "rejected", reason)
        self.db.update("moments", clip["moment_id"], status="rejected")
        self.db.add_review(clip, "rejected", reason)
        self.scheduler.cancel_clip(clip_id, "clipe rejeitado")
        log.info("clipe %d rejeitado (%s)", clip_id, reason or "sem motivo")
        return self.get(clip_id)

    def restore(self, clip_id: int) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        if not Path(clip["path"]).exists():
            raise ReviewError("arquivo do clipe já foi apagado")
        self.db.set_clip_status(clip_id, "ready", None)
        self.db.update("moments", clip["moment_id"], status="clipped")
        self.db.add_review(clip, "restored")
        self.scheduler.cancel_clip(clip_id, "clipe voltou para a fila")
        return self.get(clip_id)

    def mark_posted(self, clip_id: int, platforms: list[str] | None = None) -> dict:
        """Dono postou à mão: marca os posts agendados do clipe (ou cria registros) como postados."""
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        now = time.time()
        existing = {p["platform"]: p for p in self.db.posts_for_clip(clip_id)}
        wanted = platforms or [p for p in existing] or ["manual"]
        for p in wanted:
            if p in existing and existing[p]["status"] in ("scheduled", "failed", "skipped", "posting"):
                self.db.update("posts", existing[p]["id"], status="posted", posted_at=now, error=None)
            elif p not in existing:
                self.db.insert("posts", clip_id=clip_id, platform=p, status="posted", posted_at=now, publisher="manual",
                               attempts=0, created_at=now)
        self.db.set_clip_status(clip_id, "posted", ",".join(wanted))
        return self.get(clip_id)

    # ------------------------------------------------------------------ agenda de posts
    def list_posts(self, status: str = "scheduled,posting,failed,processing", limit: int = 200) -> list[dict]:
        out = []
        for p in self.db.posts(status=status, limit=limit):
            copy_data = {}
            clip = self.db.get_clip(p["clip_id"])
            if clip and clip.get("copy_json"):
                copy_data = json.loads(clip["copy_json"])
            net = copy_data.get("networks", {}).get(p["platform"], {})
            s = self.cfg.streamer(p["streamer"])
            out.append({
                "id": p["id"], "clip_id": p["clip_id"], "platform": p["platform"], "status": p["status"], "publisher": p.get("publisher") or "manual",
                "scheduled_at": p.get("scheduled_at"), "posted_at": p.get("posted_at"),
                "when": time.strftime("%a %d/%m %H:%M", time.localtime(p["scheduled_at"])) if p.get("scheduled_at") else "",
                "due": bool(p.get("scheduled_at") and p["scheduled_at"] <= time.time() and p["status"] == "scheduled"),
                "title": p.get("title") or "", "caption": p.get("caption") or net.get("description") or "",
                "hashtags": net.get("hashtags", []), "url": p.get("url"), "error": p.get("error"), "attempts": p.get("attempts") or 0,
                "streamer_name": s.name if s else p["streamer"], "score": round(float(p.get("score") or 0)),
                "has_cover": bool(p.get("cover_path")) and Path(p["cover_path"]).exists(),
                "video_name": self._video_name_for(p),
                "metrics": json.loads(p["metrics_json"]) if p.get("metrics_json") else None,
            })
        return out

    def _video_name_for(self, p: dict) -> str:
        if p["platform"] == "youtube" and p.get("music_path") and Path(p["music_path"]).exists():
            return "clip_youtube.mp4"
        clip = self.db.get_clip(p["clip_id"]) or {}
        long_nets = self.cfg.settings.get("edit.long_version.networks", ["tiktok"]) or []
        if p["platform"] in long_nets and clip.get("long_path") and Path(clip["long_path"]).exists():
            return "clip_long.mp4"
        return "clip.mp4"

    def post_action(self, post_id: int, action: str, url: str | None = None) -> dict:
        post = self.db.get_post(post_id)
        if not post:
            raise ReviewError("post não existe")
        if action == "posted":        # dono postou à mão
            self.db.update("posts", post_id, status="posted", posted_at=time.time(), url=url or post.get("url"), error=None)
            self.scheduler._maybe_mark_clip_posted(post["clip_id"])
        elif action == "skip":
            self.db.update("posts", post_id, status="skipped", error="pulado pelo dono")
        elif action == "now":         # publica agora (auto) ou adianta para já (manual)
            pub = self.scheduler.publishers.get(post["platform"])
            if pub is None or pub.manual:
                self.db.update("posts", post_id, status="scheduled", scheduled_at=time.time())
            else:
                self.db.update("posts", post_id, status="scheduled", scheduled_at=time.time())
                return {"post": self.scheduler.publish_post(post_id, pub)}
        elif action == "retry":
            self.db.update("posts", post_id, status="scheduled", scheduled_at=time.time(), attempts=0, error=None)
        elif action == "reschedule":
            when = self.scheduler.next_slot(post["platform"], time.time() + 600)
            if when is None:
                raise ReviewError("sem vaga nas janelas")
            self.db.update("posts", post_id, status="scheduled", scheduled_at=when, error=None)
        else:
            raise ReviewError(f"ação desconhecida: {action}")
        return {"post": self.db.get_post(post_id)}

    def publish_status(self) -> dict:
        return self.scheduler.status_summary()

    # ------------------------------------------------------------------ painel de métricas
    def metrics(self, days: int = 7) -> dict:
        """Números do pipeline: produção por dia/streamer, aprovação, latência, posts e saúde do sistema."""
        import shutil
        import subprocess

        now = time.time()
        since = now - days * 86400
        db = self.db
        per_day = db.fetchall(
            """SELECT date(c.created_at, 'unixepoch', 'localtime') AS day, COUNT(*) AS clips,
                      SUM(c.status IN ('approved','posted')) AS approved, SUM(c.status = 'rejected') AS rejected,
                      SUM(c.status = 'posted') AS posted
               FROM clips c WHERE c.created_at >= ? GROUP BY day ORDER BY day""", [since])
        per_streamer = db.fetchall(
            """SELECT s.streamer, COUNT(DISTINCT st.id) AS streams, COUNT(DISTINCT seg.id) AS segments,
                      COUNT(DISTINCT m.id) AS moments, COUNT(DISTINCT c.id) AS clips,
                      SUM(c.status IN ('approved','posted')) AS approved, SUM(c.status = 'rejected') AS rejected,
                      ROUND(AVG(m.score), 1) AS avg_score
               FROM streams s LEFT JOIN streams st ON st.id = s.id
               LEFT JOIN segments seg ON seg.stream_id = s.id
               LEFT JOIN moments m ON m.stream_id = s.id
               LEFT JOIN clips c ON c.moment_id = m.id
               WHERE s.started_at >= ? GROUP BY s.streamer ORDER BY clips DESC""", [since])
        latency = db.fetchone(
            """SELECT ROUND(AVG(c.created_at - m.peak_ts) / 60.0, 1) AS avg_min, ROUND(MIN(c.created_at - m.peak_ts) / 60.0, 1) AS min_min,
                      ROUND(MAX(c.created_at - m.peak_ts) / 60.0, 1) AS max_min, COUNT(*) AS n
               FROM clips c JOIN moments m ON m.id = c.moment_id JOIN streams s ON s.id = m.stream_id
               WHERE c.created_at >= ? AND s.url NOT LIKE '%.mp4' AND c.created_at - m.peak_ts < 6 * 3600""", [since]) or {}
        posts = db.fetchall(
            """SELECT platform, COUNT(*) AS total, SUM(status = 'posted') AS posted, SUM(status = 'failed') AS failed,
                      SUM(status = 'scheduled') AS scheduled FROM posts GROUP BY platform""")
        views = {}
        for p in db.posts(status="posted", limit=500):
            if p.get("metrics_json"):
                try:
                    m = json.loads(p["metrics_json"])
                except Exception:  # noqa: BLE001
                    continue
                v = views.setdefault(p["platform"], {"views": 0, "likes": 0, "n": 0})
                v["views"] += int(m.get("views") or m.get("plays") or 0)
                v["likes"] += int(m.get("likes") or 0)
                v["n"] += 1
        backlog = db.fetchone("SELECT SUM(status='recorded') AS to_detect, SUM(status='detecting') AS detecting FROM segments") or {}
        pending = db.fetchone("SELECT SUM(status='new') AS new, SUM(status='waiting_media') AS waiting, SUM(status='editing') AS editing FROM moments") or {}
        live = [{"streamer": s["streamer"], "since": time.strftime("%H:%M", time.localtime(s["started_at"])),
                 "segments": db.fetchone("SELECT COUNT(*) AS n FROM segments WHERE stream_id=?", [s["id"]])["n"]} for s in db.live_streams()]
        data_dir = self.cfg.settings.path("data_dir")
        usage = shutil.disk_usage(data_dir)
        gpu = None
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                u, mu, mt, tp = [x.strip() for x in out.split(",")[:4]]
                gpu = {"util": int(u), "mem_used": int(mu), "mem_total": int(mt), "temp": int(tp)}
        except Exception:  # noqa: BLE001
            pass
        rec_size = 0.0
        try:
            rec_size = sum(f.stat().st_size for f in self.cfg.settings.path("recordings_dir").rglob("*.ts")) / 1e9
        except Exception:  # noqa: BLE001
            pass
        backups = sorted((self.cfg.settings.path("data_dir") / "backups").glob("*.sqlite")) if (self.cfg.settings.path("data_dir") / "backups").exists() else []
        return {
            "days": days, "per_day": per_day, "per_streamer": per_streamer, "latency": latency, "posts": posts, "views": views,
            "reviews": db.review_stats(), "counts": db.clip_counts(),
            "health": {"live": live, "backlog": {k: int(v or 0) for k, v in backlog.items()}, "pending": {k: int(v or 0) for k, v in pending.items()},
                       "disk_free_gb": round(usage.free / 1e9, 1), "recordings_gb": round(rec_size, 2), "gpu": gpu,
                       "last_backup": time.strftime("%d/%m %H:%M", time.localtime(backups[-1].stat().st_mtime)) if backups else None,
                       "publish_mode": self.cfg.settings.get("publish.mode", "manual"),
                       "plan_b": bool(self.cfg.settings.get("capture.plan_b.enabled")),
                       "tunnel_url": __import__("clipador.review.tunnel", fromlist=["current_url"]).current_url(data_dir / "tunnel_url.txt")},
            "generated_at": time.strftime("%d/%m %H:%M:%S"),
        }

    def recut(self, clip_id: int, start_delta: float = 0.0, end_delta: float = 0.0, reedit: bool = False) -> dict:
        """Move início/fim (segundos; negativo = antes) e manda o momento de volta para a fila de edição.

        O clipe atual vira `superseded`; o orquestrador renderiza um clipe novo para o mesmo momento.
        """
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        moment = self.db.get_moment(clip["moment_id"])
        if not moment:
            raise ReviewError("momento não existe")
        start = moment["start_ts"] + float(start_delta)
        end = moment["end_ts"] + float(end_delta)
        if end - start < MIN_CLIP or end - start > MAX_CLIP:
            raise ReviewError(f"duração {end - start:.0f}s fora de {MIN_CLIP:.0f}-{MAX_CLIP:.0f}s")
        segs = [s for s in self.db.segments_covering(moment["stream_id"], start - 1, end + 1) if Path(s["path"]).exists()]
        if not segs:
            raise ReviewError("gravação bruta já apagada: não dá para recortar")
        lo, hi = min(s["start_ts"] for s in segs), max(s["end_ts"] for s in segs)
        start, end = max(start, lo), min(end, hi)
        if end - start < MIN_CLIP:
            raise ReviewError("a gravação não cobre esse intervalo")
        peak = min(max(moment["peak_ts"], start + 0.5), end - 0.5)
        self.db.update("moments", moment["id"], start_ts=start, end_ts=end, peak_ts=peak, status="new",
                       recut_count=int(moment.get("recut_count") or 0) + 1)
        self.db.set_clip_status(clip_id, "superseded", f"recorte {start_delta:+.1f}/{end_delta:+.1f}s" if not reedit else "re-edição")
        self.db.add_review(clip, "reedit" if reedit else "recut", None, start_delta=start_delta, end_delta=end_delta, moment=moment)
        log.info("clipe %d -> %s; momento %d volta para a fila (início %+.1fs, fim %+.1fs)", clip_id,
                 "re-edição" if reedit else "recorte", moment["id"], start - moment["start_ts"], end - moment["end_ts"])
        return self.get(clip_id)

    # ------------------------------------------------------------------ limpeza
    def purge_old(self, hours: float | None) -> int:
        """Apaga pastas de saída de clipes rejeitados/substituídos antigos (status -> purged)."""
        if not hours:
            return 0
        removed = 0
        for c in self.db.purgeable_clips(time.time() - float(hours) * 3600):
            d = Path(c["path"]).parent
            out_root = self.cfg.settings.path("output_dir").resolve()
            if d.exists() and out_root in d.resolve().parents:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
            self.db.update("clips", c["id"], status="purged")
        if removed:
            log.info("limpeza: %d pasta(s) de clipes rejeitados apagadas", removed)
        return removed

    def stats(self) -> dict:
        posts = self.db.post_counts()
        due = len(self.db.due_posts(time.time()))
        return {"counts": self.db.clip_counts(), "by_score": self.db.review_stats(),
                "pending_moments": len(self.db.pending_moments()), "posts": posts, "posts_due": due,
                "publish_mode": self.cfg.settings.get("publish.mode", "manual")}
