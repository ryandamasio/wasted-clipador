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
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

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
        allowed = {"clip.mp4": clip["path"], "cover.jpg": clip.get("cover_path"), "clip_youtube.mp4": clip.get("music_path")}
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
        return self.get(clip_id)

    def reject(self, clip_id: int, reason: str | None = None) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        self.db.set_clip_status(clip_id, "rejected", reason)
        self.db.update("moments", clip["moment_id"], status="rejected")
        self.db.add_review(clip, "rejected", reason)
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
        return self.get(clip_id)

    def mark_posted(self, clip_id: int, platforms: list[str] | None = None) -> dict:
        clip = self.db.get_clip(clip_id)
        if not clip:
            raise ReviewError("clipe não existe")
        self.db.set_clip_status(clip_id, "posted", ",".join(platforms or []) or None)
        for p in platforms or ["manual"]:
            self.db.insert("posts", clip_id=clip_id, platform=p, status="manual", posted_at=time.time())
        return self.get(clip_id)

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
        return {"counts": self.db.clip_counts(), "by_score": self.db.review_stats(),
                "pending_moments": len(self.db.pending_moments())}
