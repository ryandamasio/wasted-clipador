"""Agendador anti-spam e executor de publicações.

- Ao aprovar um clipe, cria um `post` por rede habilitada com horário dentro das janelas configuradas,
  respeitando limite diário (com rampa de aquecimento para conta nova), espaçamento mínimo entre posts da
  mesma rede e defasagem entre redes para o mesmo clipe (nunca tudo no mesmo minuto).
- `run_due()` (thread do orquestrador ou `clipador publish run`) publica o que venceu, com retry e backoff.
  Redes em modo manual ficam `scheduled` até o dono marcar como postado na fila web.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from clipador.config import Config
from clipador.publish.base import PostPayload, PublishError, Publisher, PublishResult
from clipador.publish.manual import ManualPublisher
from clipador.store.db import Database

log = logging.getLogger("publish.scheduler")

NETWORK_ORDER = ["youtube", "tiktok", "instagram", "kwai"]


@dataclass
class Window:
    start_min: int   # minutos desde 00:00
    end_min: int


def parse_windows(items: list[str]) -> list[Window]:
    out = []
    for it in items or ["11:30-13:30", "17:30-19:30", "20:30-23:00"]:
        a, b = it.split("-")
        h1, m1 = (int(x) for x in a.strip().split(":"))
        h2, m2 = (int(x) for x in b.strip().split(":"))
        out.append(Window(h1 * 60 + m1, h2 * 60 + m2))
    return sorted(out, key=lambda w: w.start_min)


def _day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _at_minute(day_start: float, minute: int) -> float:
    return (datetime.fromtimestamp(day_start) + timedelta(minutes=minute)).timestamp()


def build_publishers(cfg: Config) -> dict[str, Publisher]:
    """Uma instância por rede habilitada, conforme publish.networks.<rede>.via (api | upload-post | manual)."""
    p = cfg.settings.section("publish")
    tokens_dir = cfg.settings.path("data_dir") / "tokens"
    out: dict[str, Publisher] = {}
    mode = p.get("mode", "manual")
    for net, ncfg in (p.get("networks") or {}).items():
        ncfg = ncfg or {}
        if not ncfg.get("enabled", False):
            continue
        via = ncfg.get("via", "manual") if mode == "auto" else "manual"
        if via == "manual":
            pub: Publisher = ManualPublisher(ncfg, p, tokens_dir)
            pub.name = net
        elif via == "upload-post":
            from clipador.publish.uploadpost import UploadPostPublisher

            pub = UploadPostPublisher(net, ncfg, p, tokens_dir)
        else:
            if net == "youtube":
                from clipador.publish.youtube import YouTubePublisher as Cls
            elif net == "tiktok":
                from clipador.publish.tiktok import TikTokPublisher as Cls
            elif net == "instagram":
                from clipador.publish.instagram import InstagramPublisher as Cls
            else:  # kwai e outras sem API pública
                Cls = None
            if Cls is None:
                pub = ManualPublisher(ncfg, p, tokens_dir)
                pub.name = net
            else:
                pub = Cls(ncfg, p, tokens_dir)
        out[net] = pub
    return out


class Scheduler:
    def __init__(self, cfg: Config, db: Database, publishers: dict[str, Publisher] | None = None, rng: random.Random | None = None):
        self.cfg = cfg
        self.db = db
        self.p = cfg.settings.section("publish")
        self.publishers = publishers if publishers is not None else build_publishers(cfg)
        self.rng = rng or random.Random()
        self.windows = parse_windows(self.p.get("windows", []))

    # ------------------------------------------------------------------ limites
    def daily_limit(self, network: str, day_start: float) -> int:
        ramp = list(self.p.get("warmup_ramp") or [])
        base = int(self.p.get("posts_per_day", 3))
        cap = int(self.p.get("max_posts_per_day", 5))
        first = self.db.first_post_ts(network)
        if ramp and first is not None:
            day_idx = int((day_start - _day_start(first)) // 86400)
            if 0 <= day_idx < len(ramp):
                return min(int(ramp[day_idx]), cap)
        elif ramp and first is None:
            return min(int(ramp[0]), cap)
        return min(base, cap)

    def next_slot(self, network: str, after: float, now: float | None = None, max_days: int = 7) -> float | None:
        """Primeiro horário >= after dentro de uma janela com vaga no dia e espaçamento mínimo."""
        now = now or time.time()
        gap = float(self.p.get("min_gap_minutes", 90)) * 60
        t = max(after, now)
        for _ in range(max_days * len(self.windows) + 2):
            day0 = _day_start(t)
            minute = int((t - day0) // 60)
            win = next((w for w in self.windows if minute < w.end_min), None)
            if win is None:  # acabou o dia: próxima janela amanhã
                t = _at_minute(day0 + 86400, self.windows[0].start_min)
                continue
            cand = max(t, _at_minute(day0, win.start_min))
            existing = self.db.platform_posts_between(network, day0, day0 + 86400)
            if len(existing) >= self.daily_limit(network, day0):
                t = _at_minute(day0 + 86400, self.windows[0].start_min)
                continue
            clash = [e for e in existing if abs(float(e["posted_at"] or e["scheduled_at"]) - cand) < gap]
            if clash:
                t = max(float(e["posted_at"] or e["scheduled_at"]) for e in clash) + gap
                continue
            if cand <= _at_minute(day0, win.end_min):
                return cand
            t = _at_minute(day0, win.end_min) + 60
        return None

    # ------------------------------------------------------------------ agendar
    def schedule_clip(self, clip_id: int, now: float | None = None, asap: bool = False) -> list[int]:
        """Cria posts para o clipe em cada rede habilitada. asap=True ignora janelas (botão 'postar agora')."""
        now = now or time.time()
        clip = self.db.get_clip(clip_id)
        if not clip:
            return []
        copy_data = json.loads(clip["copy_json"]) if clip.get("copy_json") else {}
        nets = copy_data.get("networks", {})
        already = {p["platform"] for p in self.db.posts_for_clip(clip_id) if p["status"] in ("scheduled", "posting", "posted")}
        max_age = float(self.p.get("max_clip_age_hours", 36)) * 3600
        created: list[int] = []
        jit_lo, jit_hi = (float(x) * 60 for x in (self.p.get("jitter_minutes") or [3, 12]))
        off_lo, off_hi = (float(x) * 60 for x in (self.p.get("cross_network_offset_minutes") or [8, 35]))
        after = now + self.rng.uniform(jit_lo, jit_hi)
        order = [n for n in NETWORK_ORDER if n in self.publishers] + [n for n in self.publishers if n not in NETWORK_ORDER]
        for net in order:
            if net in already:
                continue
            data = nets.get(net) or {}
            title = (clip.get("chosen_title") or clip.get("title") or (copy_data.get("titles") or [""])[0]).strip()
            if net == "youtube" and "#shorts" not in title.lower():
                title = f"{title} #shorts"[:100]
            caption = data.get("description") or f"{title}\n\n{copy_data.get('credit', '')}"
            pub = self.publishers[net]
            publisher_kind = "manual" if pub.manual else ("upload-post" if pub.__class__.__name__ == "UploadPostPublisher" else "api")
            when = now if asap else self.next_slot(net, after, now)
            if when is None:
                pid = self.db.add_post(clip_id, net, None, title, caption, publisher_kind, status="skipped")
                self.db.update("posts", pid, error="sem vaga nas janelas dos próximos dias")
                log.warning("clipe %d/%s: sem vaga na agenda", clip_id, net)
                created.append(pid)
                continue
            if when - now > max_age and not asap:
                log.info("clipe %d/%s: só haveria vaga em %.0f h; agendado mesmo assim (velocidade importa menos que constância)",
                         clip_id, net, (when - now) / 3600)
            pid = self.db.add_post(clip_id, net, when, title, caption, publisher_kind)
            created.append(pid)
            log.info("clipe %d agendado em %s para %s (%s)", clip_id, net, time.strftime("%d/%m %H:%M", time.localtime(when)), publisher_kind)
            after = when + self.rng.uniform(off_lo, off_hi)
        return created

    def cancel_clip(self, clip_id: int, reason: str = "cancelado") -> int:
        n = 0
        for p in self.db.posts_for_clip(clip_id):
            if p["status"] in ("scheduled", "failed"):
                self.db.update("posts", p["id"], status="skipped", error=reason)
                n += 1
        return n

    # ------------------------------------------------------------------ executar
    def run_due(self, now: float | None = None) -> list[dict]:
        """Publica posts vencidos (não manuais). Retorna lista de {post_id, status, url|error}."""
        now = now or time.time()
        results = []
        for post in self.db.due_posts(now):
            pub = self.publishers.get(post["platform"])
            if pub is None or pub.manual:
                continue  # manual: fica na agenda até o dono marcar
            results.append(self.publish_post(post["id"], pub))
        return results

    def publish_post(self, post_id: int, pub: Publisher | None = None, force: bool = False) -> dict:
        post = self.db.get_post(post_id)
        if not post:
            return {"post_id": post_id, "status": "missing"}
        pub = pub or self.publishers.get(post["platform"])
        if pub is None or (pub.manual and not force):
            return {"post_id": post_id, "status": "manual"}
        if post["status"] not in ("scheduled", "failed"):
            return {"post_id": post_id, "status": post["status"]}
        clip = self.db.get_clip(post["clip_id"])
        if not clip or not Path(clip["path"]).exists():
            self.db.update("posts", post_id, status="failed", error="arquivo do clipe não existe mais")
            return {"post_id": post_id, "status": "failed", "error": "sem arquivo"}
        ok, why = pub.configured()
        if not ok:
            self.db.update("posts", post_id, status="failed", error=f"não configurado: {why}", attempts=int(post["attempts"] or 0) + 1)
            log.error("post %d (%s) não publicado: %s", post_id, post["platform"], why)
            return {"post_id": post_id, "status": "failed", "error": why}
        video = Path(clip["path"])
        if post["platform"] == "youtube" and clip.get("music_path") and Path(clip["music_path"]).exists():
            video = Path(clip["music_path"])
        copy_data = json.loads(clip["copy_json"]) if clip.get("copy_json") else {}
        tags = [t.lstrip("#") for t in (copy_data.get("networks", {}).get(post["platform"], {}).get("hashtags") or [])]
        payload = PostPayload(video=video, title=post["title"] or clip.get("title") or "", caption=post["caption"] or "", tags=tags,
                              cover=Path(clip["cover_path"]) if clip.get("cover_path") else None, clip_id=clip["id"], post_id=post_id)
        self.db.update("posts", post_id, status="posting", attempts=int(post["attempts"] or 0) + 1)
        t0 = time.time()
        try:
            res: PublishResult = pub.publish(payload)
        except PublishError as exc:
            return self._handle_failure(post, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("post %d: erro inesperado", post_id)
            return self._handle_failure(post, PublishError(str(exc)[:300], retryable=True))
        self.db.update("posts", post_id, status="posted" if res.status == "posted" else "processing", posted_at=time.time(),
                       url=res.url, remote_id=res.remote_id, error=None)
        self._maybe_mark_clip_posted(post["clip_id"])
        log.info("POST %s #%d publicado em %.0fs: %s", post["platform"], post_id, time.time() - t0, res.url or res.remote_id)
        return {"post_id": post_id, "status": "posted", "url": res.url}

    def _handle_failure(self, post: dict, exc: PublishError) -> dict:
        attempts = int(post["attempts"] or 0) + 1
        backoff = [float(x) for x in (self.p.get("retry_backoff_minutes") or [5, 20, 60, 180])]
        if exc.retryable and attempts <= len(backoff):
            when = time.time() + backoff[attempts - 1] * 60
            self.db.update("posts", post["id"], status="scheduled", scheduled_at=when, error=str(exc)[:500])
            log.warning("post %d (%s) falhou (%s); tentativa %d em %.0f min", post["id"], post["platform"], exc, attempts, backoff[attempts - 1])
            return {"post_id": post["id"], "status": "retry", "error": str(exc)}
        self.db.update("posts", post["id"], status="failed", error=str(exc)[:500])
        log.error("post %d (%s) FALHOU definitivamente: %s", post["id"], post["platform"], exc)
        return {"post_id": post["id"], "status": "failed", "error": str(exc)}

    def _maybe_mark_clip_posted(self, clip_id: int) -> None:
        posts = self.db.posts_for_clip(clip_id)
        if posts and all(p["status"] in ("posted", "processing", "skipped", "failed") for p in posts) and \
                any(p["status"] in ("posted", "processing") for p in posts):
            self.db.update("clips", clip_id, status="posted", reviewed_at=time.time())

    # ------------------------------------------------------------------ métricas
    def refresh_metrics(self, max_age_hours: float = 6.0, limit: int = 50) -> int:
        n = 0
        cutoff = time.time() - max_age_hours * 3600
        for post in self.db.posts(status="posted,processing", limit=limit):
            if post.get("metrics_at") and float(post["metrics_at"]) > cutoff:
                continue
            pub = self.publishers.get(post["platform"])
            if not pub or pub.manual or not post.get("remote_id"):
                continue
            try:
                m = pub.fetch_metrics(post["remote_id"])
            except Exception as exc:  # noqa: BLE001
                log.debug("métricas %s #%d: %s", post["platform"], post["id"], exc)
                continue
            if m:
                upd = {"metrics_json": json.dumps(m, ensure_ascii=False), "metrics_at": time.time()}
                if post["status"] == "processing" and m.get("status") in ("PUBLISH_COMPLETE", "processed", "uploaded"):
                    upd["status"] = "posted"
                if m.get("url") and not post.get("url"):
                    upd["url"] = m["url"]
                self.db.update("posts", post["id"], **upd)
                n += 1
        return n

    def status_summary(self) -> dict:
        nets = {}
        for net, pub in self.publishers.items():
            ok, why = pub.configured()
            nets[net] = {"manual": pub.manual, "configured": ok, "detail": why}
        return {"mode": self.p.get("mode", "manual"), "networks": nets, "posts": self.db.post_counts(),
                "windows": [f"{w.start_min // 60:02d}:{w.start_min % 60:02d}-{w.end_min // 60:02d}:{w.end_min % 60:02d}" for w in self.windows]}
