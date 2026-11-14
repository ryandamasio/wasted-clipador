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
        self.paused_for_disk = False

    # ------------------------------------------------------------- lifecycle
    def run(self) -> None:
        log.info("Clipador iniciado. Navegador p/ Kick: %s. Streamers ativos: %s", self.browser or "nenhum",
                 ", ".join(s.handle for s in self.cfg.enabled_streamers))
        self.recover()
        threads = [
            threading.Thread(target=self._guard(self.monitor_loop), name="monitor", daemon=True),
            threading.Thread(target=self._guard(self.detect_loop), name="detect", daemon=True),
            threading.Thread(target=self._guard(self.edit_loop), name="edit", daemon=True),
            threading.Thread(target=self._guard(self.retention_loop), name="retention", daemon=True),
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

    def stop(self) -> None:
        self.stop_event.set()
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
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="online-check")
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
        self.db.update("moments", m["id"], status="editing")
        day = time.strftime("%Y-%m-%d", time.localtime(m["start_ts"]))
        hhmmss = time.strftime("%H%M%S", time.localtime(m["start_ts"]))
        clip_name = f"{streamer.handle}_{hhmmss}_m{m['id']}"
        out_dir = self.cfg.settings.path("output_dir") / day / clip_name
        try:
            result = self.renderer.render(m, segments, streamer, out_dir, live)
        except MediaNotReady as exc:
            self.db.update("moments", m["id"], status="waiting_media")
            log.debug("momento %d aguardando mídia: %s", m["id"], exc)
            return False
        except Exception:  # noqa: BLE001
            log.exception("render do momento %d falhou", m["id"])
            self.db.update("moments", m["id"], status="failed")
            return True
        reasons = json.loads(m["reasons"]) if isinstance(m["reasons"], str) else m["reasons"]
        llm_title = (reasons.get("llm") or {}).get("title") if isinstance(reasons, dict) else None
        copy_data = generate_copy(self.cfg, streamer, m, llm_title)
        post_md = write_post_files(out_dir, copy_data, m, streamer, clip_name)
        clip_id = self.db.add_clip(m["id"], result.video, result.cover, post_md, result.duration,
                                   copy_data["titles"][0], copy_data)
        self.db.update("moments", m["id"], status="clipped")
        latency = time.time() - m["peak_ts"]
        log.info("CLIPE PRONTO #%d (%.0fs, nota %.0f, %s, %.0f min após o momento): %s", clip_id, result.duration,
                 m["score"], result.encoder, latency / 60, out_dir)
        return True

    # ------------------------------------------------------------- retention
    def retention_loop(self) -> None:
        while not self.stop_event.is_set():
            self.run_retention()
            self.stop_event.wait(1800)

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
        except Exception:  # noqa: BLE001
            log.exception("retenção falhou")
        return removed
