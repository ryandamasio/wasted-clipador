"""Gravação de uma live em segmentos .ts sem recodificar.

Pipeline: streamlink --stdout  |  ffmpeg -c copy -f segment
Fallback:  yt-dlp -o -         |  ffmpeg -c copy -f segment
Plano B:   navegador em kiosk numa região da tela + ffmpeg gdigrab (+ dshow áudio loopback) -> NVENC -> segmentos
           (entra sozinho quando streamlink e yt-dlp falham seguidas vezes e capture.plan_b.enabled = true)

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
        self.title = title or ""
        self.stream_id = db.create_stream(streamer.handle, streamer.platform, streamer.url, self.dir, title)
        self._idx = 0
        self._procs: list[subprocess.Popen] = []
        self.last_data_at = time.time()

    # ------------------------------------------------------------------ cmds
    def _source_cmd(self, use_ytdlp: bool) -> list[str]:
        if self.streamer.url.startswith("file:"):
            # "live" falsa a partir de arquivo (teste de carga): reproduz em tempo real, em loop
            path = self.streamer.url.split(":", 1)[1].lstrip("/")
            return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-stream_loop", "-1", "-i", path,
                    "-c", "copy", "-f", "mpegts", "pipe:1"]
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
                cmd += ["--webbrowser-executable", self.browser, "--webbrowser-headless=true"]
            if self.cfg.get("kick_low_latency"):
                cmd.append("--kick-low-latency")
        cmd += [self.streamer.url, str(self.cfg.get("quality", "best"))]
        return cmd

    def _segment_opts(self, run_dir: Path) -> list[str]:
        return ["-f", "segment", "-segment_time", str(self.segment_seconds), "-segment_format", "mpegts",
                "-reset_timestamps", "1", "-segment_list", str(run_dir / "segments.csv"), "-segment_list_type", "csv",
                str(run_dir / "seg_%05d.ts")]

    def _ffmpeg_cmd(self, run_dir: Path) -> list[str]:
        return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-i", "pipe:0",
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", *self._segment_opts(run_dir)]

    # ------------------------------------------------------------------ plano B (tela)
    def plan_b_cfg(self) -> dict:
        return dict(self.cfg.get("plan_b") or {})

    def _screen_ffmpeg_cmd(self, run_dir: Path) -> list[str]:
        """Captura a região da tela (gdigrab) + áudio loopback (dshow) e grava com NVENC (fallback libx264)."""
        pb = self.plan_b_cfg()
        reg = pb.get("region") or {}
        x, y = int(reg.get("x", 0)), int(reg.get("y", 0))
        w, h = int(reg.get("w", 1920)), int(reg.get("h", 1080))
        fps = int(pb.get("fps", 30))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
               "-f", "gdigrab", "-framerate", str(fps), "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}",
               "-draw_mouse", "0", "-i", "desktop"]
        audio = pb.get("audio_device")
        if audio:
            cmd += ["-f", "dshow", "-thread_queue_size", "1024", "-i", f"audio={audio}"]
        enc = pb.get("encoder", "h264_nvenc")
        if enc == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "26", "-b:v", str(pb.get("bitrate", "6M")), "-maxrate", "9M",
                    "-bufsize", "12M", "-g", str(fps * 2), "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", str(fps * 2), "-pix_fmt", "yuv420p"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
        cmd += self._segment_opts(run_dir)
        return cmd

    # --- roteamento de áudio do plano B (SoundVolumeView em tools/) ---------------------------------
    def _svv(self) -> Path | None:
        p = Path(self.plan_b_cfg().get("soundvolumeview") or (self.dir.parent.parent.parent / "tools" / "SoundVolumeView.exe"))
        return p if p.exists() else None

    def _current_default_output(self, svv: Path) -> str | None:
        """Nome do dispositivo de reprodução padrão (coluna 'Default' = 'Render' no CSV do SoundVolumeView)."""
        import csv
        import tempfile

        tmp = Path(tempfile.gettempdir()) / f"svv_{os.getpid()}.csv"
        try:
            subprocess.run([str(svv), "/scomma", str(tmp)], capture_output=True, timeout=20)
            with tmp.open(encoding="utf-8-sig", errors="replace", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("Type") == "Device" and row.get("Direction") == "Render" and "Render" in (row.get("Default") or ""):
                        return row.get("Name") or row.get("Device Name")
        except Exception as exc:  # noqa: BLE001
            log.debug("svv csv: %s", exc)
        finally:
            tmp.unlink(missing_ok=True)
        return None

    def _route_audio(self, enable: bool) -> None:
        """Plano B: manda o som do PC para o cabo virtual (e deixa 'ouvir' nos alto-falantes); restaura ao sair."""
        pb = self.plan_b_cfg()
        cable_in, cable_out = pb.get("cable_input", "CABLE Input"), pb.get("cable_output", "CABLE Output")
        if not pb.get("route_audio", True) or not pb.get("audio_device"):
            return
        svv = self._svv()
        if not svv:
            log.warning("[%s] plano B sem tools/SoundVolumeView.exe: não consigo rotear o áudio sozinho", self.streamer.handle)
            return
        try:
            if enable:
                prev = self._current_default_output(svv)
                self._prev_output = prev if prev and "CABLE" not in prev else None
                subprocess.run([str(svv), "/SetDefault", cable_in, "all"], capture_output=True, timeout=20)
                if self._prev_output:
                    subprocess.run([str(svv), "/SetPlaybackThroughDevice", cable_out, self._prev_output], capture_output=True, timeout=20)
                    subprocess.run([str(svv), "/SetListenToThisDevice", cable_out, "1"], capture_output=True, timeout=20)
                log.info("[%s] áudio do PC roteado para %s (você continua ouvindo em %s)", self.streamer.handle, cable_in, self._prev_output or "?")
            else:
                prev = getattr(self, "_prev_output", None)
                subprocess.run([str(svv), "/SetListenToThisDevice", cable_out, "0"], capture_output=True, timeout=20)
                if prev:
                    subprocess.run([str(svv), "/SetDefault", prev, "all"], capture_output=True, timeout=20)
                    log.info("[%s] áudio do PC de volta para %s", self.streamer.handle, prev)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] roteamento de áudio falhou: %s", self.streamer.handle, exc)

    def _browser_cmd(self) -> list[str] | None:
        pb = self.plan_b_cfg()
        if not self.browser or not pb.get("open_browser", True):
            return None
        reg = pb.get("region") or {}
        x, y = int(reg.get("x", 0)), int(reg.get("y", 0))
        w, h = int(reg.get("w", 1920)), int(reg.get("h", 1080))
        profile = Path(pb.get("profile_dir") or (self.dir.parent.parent / "browser_profile"))
        profile.mkdir(parents=True, exist_ok=True)
        url = pb.get("url_override", {}).get(self.streamer.handle) or self.streamer.url
        return [self.browser, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", "--kiosk",
                f"--window-position={x},{y}", f"--window-size={w},{h}", "--autoplay-policy=no-user-gesture-required",
                "--disable-infobars", "--disable-session-crashed-bubble", url]

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        attempts = 0
        max_attempts = int(self.cfg.get("max_reconnect_attempts", 20))
        delay = float(self.cfg.get("reconnect_delay_seconds", 15))
        pb = self.plan_b_cfg()
        plan_b_after = int(pb.get("after_failures", 2))
        mode = "screen" if (pb.get("enabled") and pb.get("force")) else "streamlink"
        quick_failures = 0
        status = "ended"
        try:
            while not self.stop_event.is_set():
                ok = self._record_once(mode)
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
                    quick_failures += 1
                    if mode != "screen" and pb.get("enabled") and quick_failures >= plan_b_after:
                        log.error("[%s] streamlink e yt-dlp falharam %d vezes: PLANO B (captura de tela)", self.streamer.handle, quick_failures)
                        mode = "screen"
                    elif mode != "screen":
                        mode = "ytdlp" if mode == "streamlink" else "streamlink"   # alterna fonte quando falha rápido
                else:
                    quick_failures = 0
                log.warning("[%s] reconectando em %.0fs (tentativa %d, fonte=%s)", self.streamer.handle, delay, attempts, mode)
                self.stop_event.wait(delay)
        finally:
            self.db.end_stream(self.stream_id, status)
            log.info("[%s] gravação encerrada (stream_id=%d, %d segmentos)", self.streamer.handle, self.stream_id, self._idx)

    def _record_once(self, mode: str | bool) -> bool:
        """Roda uma sessão de captura até cair. Retorna True se gravou por tempo razoável.

        mode: "streamlink" | "ytdlp" | "screen" (bool mantido por compatibilidade: True = ytdlp).
        """
        if isinstance(mode, bool):
            mode = "ytdlp" if mode else "streamlink"
        run_dir = self.dir / f"run_{int(time.time())}"
        run_dir.mkdir(exist_ok=True)
        creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        log.info("[%s] iniciando captura (%s) -> %s", self.streamer.handle, mode, run_dir)
        browser_proc = None
        if mode == "screen":
            self._route_audio(True)
            bcmd = self._browser_cmd()
            if bcmd:
                browser_proc = subprocess.Popen(bcmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(float(self.plan_b_cfg().get("browser_warmup_seconds", 12)))  # player abrir e começar a tocar
            ff = subprocess.Popen(self._screen_ffmpeg_cmd(run_dir), stderr=subprocess.PIPE, creationflags=creation)
            src = browser_proc or ff
            self._procs = [p for p in (browser_proc, ff) if p]
            procs_to_drain = (ff,)
        else:
            src_cmd = self._source_cmd(mode == "ytdlp")
            src = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creation)
            ff = subprocess.Popen(self._ffmpeg_cmd(run_dir), stdin=src.stdout, stderr=subprocess.PIPE, creationflags=creation)
            src.stdout.close()  # ffmpeg é o dono do pipe
            self._procs = [src, ff]
            procs_to_drain = (src, ff)
        t0 = time.time()
        started = t0
        csv_path = run_dir / "segments.csv"
        seen = 0
        threading.Thread(target=self._drain_stderr, args=procs_to_drain, daemon=True).start()
        try:
            while not self.stop_event.is_set():
                seen = self._register_new_segments(csv_path, seen, t0, final=False)
                if ff.poll() is not None or src.poll() is not None:
                    break
                time.sleep(2)
        finally:
            self._terminate(*self._procs)
            if mode == "screen":
                self._route_audio(False)
            time.sleep(1)
            self._register_new_segments(csv_path, seen, t0, final=True)
        duration = time.time() - started
        return duration > 60

    def _drain_stderr(self, *procs: subprocess.Popen) -> None:
        for proc, name in zip(procs, ("src", "ffmpeg") if len(procs) == 2 else ("ffmpeg",)):
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
