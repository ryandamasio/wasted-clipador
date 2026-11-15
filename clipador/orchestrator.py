"""Orquestrador: monitora lives, grava, detecta, edita e entrega clipes prontos na pasta de saída.

Threads:
  - monitor:   checa quem está online e inicia/para gravadores (+ chat)
  - detect:    processa segmentos gravados (1 por vez: Whisper na GPU)
  - edit:      renderiza momentos pendentes (1 por vez: NVENC) e gera textos
  - retention: apaga gravações antigas já processadas; vigia espaço em disco
Tudo se comunica pelo SQLite, então o processo pode cair e retomar de onde parou.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clipador.capture.chat import ChatRecorder, make_chat_recorder
from clipador.capture.monitor import check_online, find_browser
from clipador.capture.recorder import Recorder, free_space_gb
from clipador.config import Config, Streamer
from clipador.copy.generator import generate_copy, write_post_files
from clipador.detect.pipeline import Detector
from clipador.edit.render import MediaNotReady, Renderer
from clipador.store.db import Database

log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.cap = cfg.settings.section("capture")
        self.browser = find_browser(self.cap.get("browser_executable", "auto"))
        self.stop_event = threading.Event()
        self.recorders: dict[str, tuple[Recorder, ChatRecorder | None]] = {}
        self._rec_lock = threading.Lock()
        self.detector: Detector | None = None
        self.renderer: Renderer | None = None
        self.review_server = None
        self._scheduler = None
        self.paused_for_disk = False

    # ------------------------------------------------------------- lifecycle
    def run(self) -> None:
        log.info("Clipador iniciado. Navegador p/ Kick: %s. Streamers ativos: %s", self.browser or "nenhum",
                 ", ".join(s.handle for s in self.cfg.enabled_streamers))
        self.recover()
        if self.cfg.settings.get("review.enabled", True):
            self.start_review_server()
        threads = [
            threading.Thread(target=self._guard(self.monitor_loop), name="monitor", daemon=True),
            threading.Thread(target=self._guard(self.detect_loop), name="detect", daemon=True),
            threading.Thread(target=self._guard(self.edit_loop), name="edit", daemon=True),
            threading.Thread(target=self._guard(self.retention_loop), name="retention", daemon=True),
            threading.Thread(target=self._guard(self.publish_loop), name="publish", daemon=True),
        ]
        for t in threads:
            t.start()
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Ctrl+C: encerrando...")
        finally:
            self.stop()

    @property
    def scheduler(self):
        if self._scheduler is None:
            from clipador.publish.scheduler import Scheduler

            self._scheduler = Scheduler(self.cfg, self.db)
        return self._scheduler

    def start_review_server(self) -> None:
        try:
            from clipador.review.app import ReviewServer

            self.review_server = ReviewServer(self.cfg, self.db, self.scheduler)
            self.review_server.start()
        except Exception:  # noqa: BLE001
            log.exception("fila de aprovação não subiu (o resto continua)")

    # --------------------------------------------------------------- publish
    def publish_loop(self) -> None:
        """Publica posts vencidos (modo auto) e atualiza métricas de vez em quando."""
        poll = float(self.cfg.settings.get("publish.publish_poll_seconds", 60))
        last_metrics = 0.0
        while not self.stop_event.is_set():
            try:
                if self.cfg.settings.get("publish.mode", "manual") == "auto":
                    self.scheduler.run_due()
                    if time.time() - last_metrics > 3600:
                        self.scheduler.refresh_metrics()
                        last_metrics = time.time()
            except Exception:  # noqa: BLE001
                log.exception("ciclo de publicação falhou")
            self.stop_event.wait(poll)

    def stop(self) -> None:
        self.stop_event.set()
        if self.review_server:
            self.review_server.stop()
        with self._rec_lock:
            for rec, chat in self.recorders.values():
                rec.stop()
                if chat:
                    chat.stop()
            for rec, _chat in self.recorders.values():
                rec.join(timeout=15)
            self.recorders.clear()

    def _guard(self, fn):
        def wrapper():
            while not self.stop_event.is_set():
                try:
                    fn()
                    return
                except Exception:  # noqa: BLE001
                    log.exception("thread %s caiu; reiniciando em 10s", fn.__name__)
                    self.stop_event.wait(10)
        return wrapper

    def recover(self) -> None:
        """Após queda: streams 'live' órfãs viram 'ended'; trabalho em andamento volta para a fila."""
        for s in self.db.live_streams():
            self.db.end_stream(s["id"], "ended")
        self.db.execute("UPDATE segments SET status='recorded' WHERE status='detecting'")
        self.db.execute("UPDATE moments SET status='new' WHERE status='editing'")

    # --------------------------------------------------------------- monitor
    def monitor_loop(self) -> None:
        interval = float(self.cap.get("poll_interval_seconds", 60))
        pool = ThreadPoolExecutor(max_workers=int(self.cap.get("check_workers", 6)), thread_name_prefix="online-check")
        while not self.stop_event.is_set():
            self._reap_recorders()
            self._check_disk()
            if not self.paused_for_disk:
                active = set(self.recorders)
                candidates = [s for s in self.cfg.enabled_streamers if s.handle not in active]
                slots = int(self.cap.get("max_concurrent", 3)) - len(active)
                if candidates and slots > 0:
                    results = list(pool.map(lambda s: (s, *self._safe_check(s)), candidates))
                    for s, online, title in results:
                        if online and slots > 0:
                            self._start_recording(s, title)
                            slots -= 1
                        elif online:
                            log.info("[%s] online mas sem slot livre (max_concurrent=%s)", s.handle,
                                     self.cap.get("max_concurrent"))
            self.stop_event.wait(interval)

    def _safe_check(self, s: Streamer) -> tuple[bool, str | None]:
        try:
            return check_online(s.url, s.platform, self.browser)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] erro ao checar online: %s", s.handle, str(exc)[:160])
            return False, None

    def _start_recording(self, s: Streamer, title: str | None) -> None:
        with self._rec_lock:
            if s.handle in self.recorders:
                return
            rec = Recorder(self.db, s, self.cap, self.cfg.settings.path("recordings_dir"), self.browser, title=title)
            chat = None
            if self.cap.get("record_chat", True):
                chat = make_chat_recorder(self.db, rec.stream_id, s)
                chat.start()
            rec.start()
            self.recorders[s.handle] = (rec, chat)
            log.info("[%s] ONLINE (%s) -> gravando stream_id=%d", s.handle, title or "sem título", rec.stream_id)

    def _reap_recorders(self) -> None:
        with self._rec_lock:
            for handle in list(self.recorders):
                rec, chat = self.recorders[handle]
                if not rec.is_alive():
                    if chat:
                        chat.stop()
                    del self.recorders[handle]
                    log.info("[%s] gravador finalizado", handle)

    def _check_disk(self) -> None:
        free = free_space_gb(self.cfg.settings.path("data_dir"))
        min_free = float(self.cap.get("min_free_gb", 20))
        if free < min_free and not self.paused_for_disk:
            log.error("DISCO QUASE CHEIO: %.1f GB livres (< %.0f). Pausando novas capturas.", free, min_free)
            self.paused_for_disk = True
        elif free >= min_free * 1.5 and self.paused_for_disk:
            log.info("espaço recuperado (%.1f GB); retomando capturas", free)
            self.paused_for_disk = False

    # ---------------------------------------------------------------- detect
    def detect_loop(self) -> None:
        self.detector = self.detector or Detector(self.cfg, self.db)
        while not self.stop_event.is_set():
            seg = self.db.next_segment_to_detect()
            if not seg:
                self.stop_event.wait(3)
                continue
            self.detect_one(seg)

    def detect_one(self, seg: dict) -> list[int]:
        self.detector = self.detector or Detector(self.cfg, self.db)
        t0 = time.time()
        try:
            created = self.detector.detect_segment(seg)
            log.info("segmento %d detectado em %.1fs -> %d momento(s)", seg["id"], time.time() - t0, len(created))
            return created
        except Exception:  # noqa: BLE001
            log.exception("detecção do segmento %d falhou", seg["id"])
            self.db.update("segments", seg["id"], status="failed")
            return []

    # ------------------------------------------------------------------ edit
    def edit_loop(self) -> None:
        self.renderer = self.renderer or Renderer(self.cfg)
        while not self.stop_event.is_set():
            pending = self.db.pending_moments()
            if not pending:
                self.stop_event.wait(3)
                continue
            did_work = False
            for m in pending:
                if self.stop_event.is_set():
                    break
                if self.process_moment(m):
                    did_work = True
            if not did_work:
                self.stop_event.wait(5)

    def process_moment(self, m: dict) -> bool:
        """Renderiza um momento e escreve a pasta de saída. Retorna True se fez trabalho (sucesso ou falha)."""
        self.renderer = self.renderer or Renderer(self.cfg)
        stream = self.db.get_stream(m["stream_id"])
        streamer = self.cfg.streamer(stream["streamer"]) if stream else None
        if not stream or not streamer:
            self.db.update("moments", m["id"], status="failed")
            return True
        live = stream["status"] == "live"
        segments = self.db.segments_covering(stream["id"], m["start_ts"] - 1, m["end_ts"] + 1)
        long_max = float(self.cfg.settings.get("edit.long_version.max_seconds", 95))
        long_segments = self.db.segments_covering(stream["id"], m["start_ts"] - 1, m["start_ts"] + long_max + 1)
        self.db.update("moments", m["id"], status="editing")
        day = time.strftime("%Y-%m-%d", time.localtime(m["start_ts"]))
        hhmmss = time.strftime("%H%M%S", time.localtime(m["start_ts"]))
        clip_name = f"{streamer.handle}_{hhmmss}_m{m['id']}"
        out_dir = self.cfg.settings.path("output_dir") / day / clip_name
        try:
            result = self.renderer.render(m, segments, streamer, out_dir, live, long_segments=long_segments)
        except MediaNotReady as exc:
            if not live:
                # live acabou (ou é arquivo) e a mídia não existe mais: não adianta esperar
                self.db.update("moments", m["id"], status="failed")
                log.warning("momento %d sem mídia disponível (%s): marcado como failed", m["id"], exc)
                return True
            self.db.update("moments", m["id"], status="waiting_media")
            log.debug("momento %d aguardando mídia: %s", m["id"], exc)
            return False
        except Exception:  # noqa: BLE001
            log.exception("render do momento %d falhou", m["id"])
            self.db.update("moments", m["id"], status="failed")
            return True
        reasons = json.loads(m["reasons"]) if isinstance(m["reasons"], str) else m["reasons"]
        llm_title = (reasons.get("llm") or {}).get("title") if isinstance(reasons, dict) else None
        copy_data = generate_copy(self.cfg, streamer, m, llm_title, music=result.music)
        if result.long_video:
            copy_data["long_version"] = {"file": result.long_video.name, "seconds": round(result.long_duration or 0),
                                         "networks": list(self.cfg.settings.get("edit.long_version.networks", ["tiktok"]))}
        post_md = write_post_files(out_dir, copy_data, m, streamer, clip_name)
        render_info = {"layout": result.layout, "zoom": result.zoom_peaks, "margin_v": result.margin_v, "encoder": result.encoder,
                       "long_seconds": result.long_duration}
        clip_id = self.db.add_clip(m["id"], result.video, result.cover, post_md, result.duration,
                                   copy_data["titles"][0], copy_data, result.music_video, render_info, result.long_video)
        self.db.update("moments", m["id"], status="clipped")
        latency = time.time() - m["peak_ts"]
        log.info("CLIPE PRONTO #%d (%.0fs, nota %.0f, %s, %s, %.0f min após o momento): %s", clip_id, result.duration,
                 m["score"], result.encoder, result.layout, latency / 60, out_dir)
        auto_min = self.cfg.settings.get("review.auto_approve_min_score")
        if auto_min is not None and float(m["score"]) >= float(auto_min):
            self.db.update("clips", clip_id, status="approved", chosen_title=copy_data["titles"][0], reviewed_at=time.time(),
                           review_note=f"auto (nota >= {auto_min})")
            self.db.add_review({"id": clip_id, "moment_id": m["id"]}, "auto", f"nota >= {auto_min}",
                               chosen_title=copy_data["titles"][0], moment=m)
            log.info("clipe #%d auto-aprovado (nota %.0f >= %s)", clip_id, m["score"], auto_min)
            try:
                self.scheduler.schedule_clip(clip_id)
            except Exception:  # noqa: BLE001
                log.exception("agendamento do clipe %d falhou", clip_id)
        return True

    # ------------------------------------------------------------- retention
    def retention_loop(self) -> None:
        while not self.stop_event.is_set():
            self.run_retention()
            self.maybe_backup()
            self.stop_event.wait(1800)

    def maybe_backup(self) -> Path | None:
        """Backup do SQLite a cada capture.backup_db_hours (padrão 24 h) em data/backups."""
        hours = float(self.cap.get("backup_db_hours", 24) or 0)
        if hours <= 0:
            return None
        dest = self.cfg.settings.path("data_dir") / "backups"
        last = self.db.last_backup(dest)
        if last and time.time() - last.stat().st_mtime < hours * 3600:
            return None
        try:
            out = self.db.backup(dest, keep=7)
            log.info("backup do banco: %s", out.name)
            return out
        except Exception:  # noqa: BLE001
            log.exception("backup do banco falhou")
            return None

    def run_retention(self) -> int:
        hours = float(self.cap.get("retention_hours", 12))
        removed = 0
        try:
            cutoff = time.time() - hours * 3600
            for seg in self.db.old_processed_segments(cutoff):
                p = Path(seg["path"])
                if p.exists():
                    p.unlink(missing_ok=True)
                    removed += 1
                p.with_suffix(".transcript.json").unlink(missing_ok=True)
                self.db.update("segments", seg["id"], status="deleted")
            if removed:
                log.info("retenção: %d segmentos brutos apagados", removed)
            rec_dir = self.cfg.settings.path("recordings_dir")
            for d in rec_dir.glob("*/run_*"):
                if d.is_dir() and not any(d.glob("*.ts")):
                    shutil.rmtree(d, ignore_errors=True)
            # pastas de clipes rejeitados/substituídos antigos
            from clipador.review.service import ReviewService

            ReviewService(self.cfg, self.db).purge_old(self.cfg.settings.get("review.purge_after_hours"))
        except Exception:  # noqa: BLE001
            log.exception("retenção falhou")
        return removed
