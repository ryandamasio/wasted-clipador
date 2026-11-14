"""Gravação de uma live em segmentos .ts sem recodificar.

Pipeline: streamlink --stdout  |  ffmpeg -c copy -f segment
Fallback:  yt-dlp -o -         |  ffmpeg -c copy -f segment

Cada segmento finalizado é registrado no banco com timestamp absoluto estimado
(start_ts = instante em que o ffmpeg começou a receber dados + offset relativo do segmento).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from clipador.capture.monitor import check_online, streamlink_cmd, ytdlp_cmd
from clipador.config import Streamer
from clipador.store.db import Database

log = logging.getLogger("capture.recorder")

SegmentCallback = Callable[[int], None]  # recebe segment_id


class Recorder(threading.Thread):
    def __init__(self, db: Database, streamer: Streamer, capture_cfg: dict, recordings_dir: Path,
                 browser: str | None, on_segment: SegmentCallback | None = None, title: str | None = None):
        super().__init__(name=f"rec-{streamer.handle}", daemon=True)
        self.db = db
        self.streamer = streamer
        self.cfg = capture_cfg
        self.browser = browser
        self.on_segment = on_segment
        self.stop_event = threading.Event()
        self.segment_seconds = int(capture_cfg.get("segment_seconds", 120))
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.dir = recordings_dir / f"{streamer.handle}_{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stream_id = db.create_stream(streamer.handle, streamer.platform, streamer.url, self.dir, title)
        self._idx = 0
        self._procs: list[subprocess.Popen] = []
        self.last_data_at = time.time()

    # ------------------------------------------------------------------ cmds
    def _source_cmd(self, use_ytdlp: bool) -> list[str]:
        if use_ytdlp:
            return [*ytdlp_cmd(), "--quiet", "--no-warnings", "--no-part", "--hls-use-mpegts", "-o", "-",
                    "-f", "best", self.streamer.url]
        cmd = [*streamlink_cmd(), "--stdout", "--retry-open", "3", "--retry-streams", "5", "--retry-max", "6",
               "--stream-segment-threads", "2", "--hls-live-edge", "3", "--loglevel", "warning"]
        if self.streamer.platform == "twitch" and self.cfg.get("twitch_disable_ads", True):
            cmd.append("--twitch-disable-ads")
            cmd.append("--twitch-low-latency")
        if self.streamer.platform == "kick":
            if self.browser:
                cmd += ["--webbrowser-executable", self.browser, "--webbrowser-headless"]
            if self.cfg.get("kick_low_latency"):
                cmd.append("--kick-low-latency")
        cmd += [self.streamer.url, str(self.cfg.get("quality", "best"))]
        return cmd

    def _ffmpeg_cmd(self, run_dir: Path) -> list[str]:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy",
            "-f", "segment", "-segment_time", str(self.segment_seconds),
            "-segment_format", "mpegts", "-reset_timestamps", "1",
            "-segment_list", str(run_dir / "segments.csv"), "-segment_list_type", "csv",
            str(run_dir / "seg_%05d.ts"),
        ]

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        attempts = 0
        max_attempts = int(self.cfg.get("max_reconnect_attempts", 20))
        delay = float(self.cfg.get("reconnect_delay_seconds", 15))
        use_ytdlp = False
        status = "ended"
        try:
            while not self.stop_event.is_set():
                ok = self._record_once(use_ytdlp)
                if self.stop_event.is_set():
                    break
                online, _ = check_online(self.streamer.url, self.streamer.platform, self.browser)
                if not online:
                    log.info("[%s] live terminou", self.streamer.handle)
                    break
                attempts += 1
                if attempts > max_attempts:
                    log.error("[%s] excedeu tentativas de reconexão", self.streamer.handle)
                    status = "failed"
                    break
                if not ok:
                    use_ytdlp = not use_ytdlp  # alterna fonte quando falha rápido
                log.warning("[%s] reconectando em %.0fs (tentativa %d, fonte=%s)", self.streamer.handle, delay,
                            attempts, "yt-dlp" if use_ytdlp else "streamlink")
                self.stop_event.wait(delay)
        finally:
            self.db.end_stream(self.stream_id, status)
            log.info("[%s] gravação encerrada (stream_id=%d, %d segmentos)", self.streamer.handle, self.stream_id, self._idx)

    def _record_once(self, use_ytdlp: bool) -> bool:
        """Roda uma sessão de captura até cair. Retorna True se gravou por tempo razoável."""
        run_dir = self.dir / f"run_{int(time.time())}"
        run_dir.mkdir(exist_ok=True)
        src_cmd = self._source_cmd(use_ytdlp)
        ff_cmd = self._ffmpeg_cmd(run_dir)
        log.info("[%s] iniciando captura (%s) -> %s", self.streamer.handle, "yt-dlp" if use_ytdlp else "streamlink", run_dir)
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        src = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creation)
        ff = subprocess.Popen(ff_cmd, stdin=src.stdout, stderr=subprocess.PIPE, creationflags=creation)
        src.stdout.close()  # ffmpeg é o dono do pipe
        self._procs = [src, ff]
        t0 = time.time()
        started = t0
        csv_path = run_dir / "segments.csv"
        seen = 0
        err_thread = threading.Thread(target=self._drain_stderr, args=(src, ff), daemon=True)
        err_thread.start()
        try:
            while not self.stop_event.is_set():
                seen = self._register_new_segments(csv_path, seen, t0, final=False)
                if ff.poll() is not None or src.poll() is not None:
                    break
                time.sleep(2)
        finally:
            self._terminate(src, ff)
            time.sleep(1)
            self._register_new_segments(csv_path, seen, t0, final=True)
        duration = time.time() - started
        return duration > 60

    def _drain_stderr(self, src: subprocess.Popen, ff: subprocess.Popen) -> None:
        for proc, name in ((src, "src"), (ff, "ffmpeg")):
            def reader(p=proc, n=name):
                try:
                    for raw in iter(p.stderr.readline, b""):
                        line = raw.decode("utf-8", "replace").strip()
                        if line:
                            log.debug("[%s/%s] %s", self.streamer.handle, n, line)
                            if "error" in line.lower():
                                log.warning("[%s/%s] %s", self.streamer.handle, n, line[:200])
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=reader, daemon=True).start()

    def _register_new_segments(self, csv_path: Path, seen: int, t0: float, final: bool) -> int:
        if not csv_path.exists():
            return seen
        try:
            lines = [ln for ln in csv_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        except OSError:
            return seen
        for line in lines[seen:]:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            fname, start_rel, end_rel = parts[0], float(parts[1]), float(parts[2])
            path = csv_path.parent / fname
            if not path.exists() or path.stat().st_size < 50_000:
                continue
            if end_rel - start_rel < 5:  # fragmento inútil
                continue
            start_ts = t0 + start_rel
            end_ts = t0 + end_rel
            seg_id = self.db.add_segment(self.stream_id, self._idx, path, start_ts, end_ts)
            self._idx += 1
            self.last_data_at = time.time()
            log.info("[%s] segmento #%d pronto (%.0fs) %s", self.streamer.handle, self._idx - 1, end_rel - start_rel, path.name)
            if self.on_segment:
                try:
                    self.on_segment(seg_id)
                except Exception:  # noqa: BLE001
                    log.exception("callback de segmento falhou")
        return len(lines)

    def _terminate(self, *procs: subprocess.Popen) -> None:
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except OSError:
                    pass
        deadline = time.time() + 8
        for p in procs:
            while p.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass

    def stop(self) -> None:
        self.stop_event.set()
        self._terminate(*self._procs)


def free_space_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.free / 1e9
