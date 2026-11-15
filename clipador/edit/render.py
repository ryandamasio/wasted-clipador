"""Renderização do clipe vertical 9:16 com ffmpeg (NVENC, fallback libx264).

Entrada: momento (timestamps absolutos) + segmentos que cobrem o intervalo.
Saída: clip.mp4 (sem música, para TikTok/Instagram/Kwai), clip_youtube.mp4 (com música livre, opcional),
cover.jpg e subs.ass em um diretório do clipe.

Fase 2: zoom dinâmico (punch-in suave nos picos de áudio), layout dividido facecam + gameplay quando o
streamer tem `facecam` configurado, música livre com ducking sob a voz.
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from clipador.config import Config, Streamer
from clipador.detect.audio import decode_audio, rms_db_series
from clipador.detect.transcribe import Transcriber, Word
from clipador.edit.subtitles import build_ass

log = logging.getLogger("edit.render")

MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac")


class MediaNotReady(Exception):
    """Os segmentos ainda não cobrem o fim do clipe (live em andamento)."""


@dataclass
class RenderResult:
    video: Path
    cover: Path | None
    duration: float
    words: list[Word]
    encoder: str
    music_video: Path | None = None
    music: dict | None = None
    zoom_peaks: list[float] = field(default_factory=list)
    layout: str = "center"
    margin_v: int = 560
    long_video: Path | None = None
    long_duration: float | None = None


def _ff_escape(path: Path | str) -> str:
    """Escapa caminho para uso dentro de filter_complex (Windows: C\\:/dir/file)."""
    s = str(path).replace("\\", "/")
    s = s.replace(":", "\\:")
    return s.replace("'", "\\'")


def _text_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def _even(n: float) -> int:
    return max(2, int(round(n / 2.0)) * 2)


def build_concat(segments: list[dict], out: Path) -> None:
    lines = ["ffconcat version 1.0"]
    for s in segments:
        p = str(Path(s["path"]).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{p}'")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def source_for(moment: dict, segments: list[dict], stream_live: bool) -> tuple[list[dict], float]:
    """Seleciona segmentos contíguos que cobrem [start,end] e devolve (segmentos, offset do início)."""
    if not segments:
        raise MediaNotReady("nenhum segmento cobre o momento")
    segs = sorted(segments, key=lambda s: s["idx"])
    first = segs[0]
    if first["start_ts"] > moment["start_ts"] + 0.5:
        # início do clipe antes do primeiro segmento disponível: recorta
        moment = {**moment, "start_ts": first["start_ts"]}
    last = segs[-1]
    if last["end_ts"] < moment["end_ts"] - 0.75:
        if stream_live:
            raise MediaNotReady(f"faltam {moment['end_ts'] - last['end_ts']:.0f}s de mídia")
        moment = {**moment, "end_ts": last["end_ts"]}
    # garante contiguidade (sem buracos entre runs de reconexão)
    chosen = [segs[0]]
    for s in segs[1:]:
        if abs(s["start_ts"] - chosen[-1]["end_ts"]) > 3.0:
            if chosen[-1]["end_ts"] >= moment["end_ts"] - 0.75:
                break
            raise MediaNotReady("buraco na gravação dentro do clipe")
        chosen.append(s)
    offset = moment["start_ts"] - chosen[0]["start_ts"]
    return chosen, max(0.0, offset)


def probe_video_size(path: Path) -> tuple[int, int]:
    """(largura, altura) do primeiro stream de vídeo; 1920x1080 se falhar."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-analyzeduration", "20M", "-probesize", "50M", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60)
        st = json.loads(out.stdout)["streams"][0]
        w, h = int(st["width"]), int(st["height"])
        if w > 0 and h > 0:
            return w, h
    except Exception as exc:  # noqa: BLE001
        log.warning("ffprobe falhou em %s: %s", path, exc)
    return 1920, 1080


# ---------------------------------------------------------------- zoom dinâmico
def pick_zoom_peaks(audio: np.ndarray, duration: float, must_include: float | None, max_peaks: int = 3,
                    min_gap: float = 4.0, edge: float = 1.0) -> list[float]:
    """Instantes (s, relativos ao clipe) onde o volume salta: candidatos a punch-in.

    Sempre inclui `must_include` (pico detectado) quando dentro do clipe.
    """
    peaks: list[float] = []
    if must_include is not None and edge <= must_include <= duration - edge:
        peaks.append(round(float(must_include), 2))
    times, db = rms_db_series(audio, 0.25)
    if len(db) >= 8:
        med = float(np.median(db))
        std = float(np.std(db)) or 1.0
        thr = med + max(1.0 * std, 3.0)
        order = np.argsort(db)[::-1]
        for i in order:
            if len(peaks) >= max_peaks:
                break
            t = float(times[i]) + 0.125
            if db[i] < thr or t < edge or t > duration - edge:
                continue
            if all(abs(t - p) >= min_gap for p in peaks):
                peaks.append(round(t, 2))
    return sorted(peaks)


def zoom_expression(peaks: list[float], fps: int, amount: float = 0.12, width: float = 1.2) -> str:
    """Expressão de zoom (zoompan, variável `in` = nº do frame) com um sino de cosseno em cada pico.

    z = 1 + amount * Σ bump(t - pico); bump suave de largura total 2*width, pico no instante do pico.
    """
    if not peaks:
        return "1"
    terms = []
    for p in peaks:
        terms.append(f"if(lt(abs(in/{fps}-{p:.2f}),{width:.2f}),0.5+0.5*cos(PI*(in/{fps}-{p:.2f})/{width:.2f}),0)")
    return f"1+{amount:.3f}*({'+'.join(terms)})"


# ---------------------------------------------------------------- música livre
def list_music(music_dir: Path) -> list[Path]:
    if not music_dir.exists():
        return []
    return sorted(p for p in music_dir.iterdir() if p.suffix.lower() in MUSIC_EXTS and p.is_file())


def read_music_licenses(music_dir: Path) -> dict[str, dict]:
    """Lê a tabela de LICENSES.md: | arquivo | título | autor | fonte | licença | atribuição |."""
    out: dict[str, dict] = {}
    path = music_dir / "LICENSES.md"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0].lower() in ("arquivo", "file") or set(cells[0]) <= set("-: "):
            continue
        fname = cells[0].strip("`")
        out[fname] = {"file": fname, "title": cells[1], "author": cells[2], "source": cells[3], "license": cells[4],
                      "attribution": cells[5] if len(cells) > 5 else ""}
    return out


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, timeout=60)
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


class Renderer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.e = cfg.settings.section("edit")
        self.transcriber = Transcriber.get(cfg.settings.get("detect.whisper", {}))

    def render(self, moment: dict, segments: list[dict], streamer: Streamer, out_dir: Path, stream_live: bool,
               long_segments: list[dict] | None = None) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        chosen, offset = source_for(moment, segments, stream_live)
        duration = min(moment["end_ts"], chosen[-1]["end_ts"]) - max(moment["start_ts"], chosen[0]["start_ts"])
        duration = max(5.0, round(duration, 2))
        concat = out_dir / "source.ffconcat"
        build_concat(chosen, concat)
        src_w, src_h = probe_video_size(Path(chosen[0]["path"]))

        # 1) transcrição precisa do próprio clipe (timestamps relativos ao clipe)
        t0 = time.time()
        audio = decode_audio(Path(), start=offset, duration=duration, concat_list=concat)
        transcript = self.transcriber.transcribe(audio)
        words = [w for w in transcript.words if w.end <= duration + 0.5]
        log.info("clipe transcrito em %.1fs (%d palavras)", time.time() - t0, len(words))

        # 2) legenda ASS (variação de posição entre clipes)
        subs = self.e.get("subtitles", {})
        margin_v = int(random.choice(subs.get("margin_v_options", [560])))
        ass_path = out_dir / "subs.ass"
        ass_path.write_text(build_ass(words, subs, int(self.e.get("width", 1080)), int(self.e.get("height", 1920)), margin_v),
                            encoding="utf-8")

        # 3) zoom dinâmico nos picos de áudio
        zoom_cfg = self.e.get("zoom", {})
        peaks: list[float] = []
        if zoom_cfg.get("enabled", True) and len(audio) > 0:
            peak_rel = moment["peak_ts"] - max(moment["start_ts"], chosen[0]["start_ts"])
            peaks = pick_zoom_peaks(audio, duration, peak_rel, int(zoom_cfg.get("max_per_clip", 3)),
                                    float(zoom_cfg.get("min_gap_seconds", 4.0)))

        # 4) render
        video = out_dir / "clip.mp4"
        layout = "split" if streamer.facecam else "center"
        encoder = self._render_video(concat, offset, duration, ass_path, streamer, video, out_dir, (src_w, src_h), peaks)

        # 5) capa no pico
        cover = out_dir / "cover.jpg"
        peak_rel = max(0.5, min(duration - 0.5, moment["peak_ts"] - moment["start_ts"]))
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{peak_rel:.2f}", "-i", str(video),
                            "-frames:v", "1", "-q:v", "3", str(cover)], check=True, capture_output=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log.warning("capa falhou: %s", exc)
            cover = None

        # 6) versão YouTube com música livre (ducking); clip.mp4 continua sem música
        music_video, music = self._music_version(video, out_dir, moment, duration)

        # 7) versão longa (> 1 min) para redes que só monetizam vídeos longos
        long_video, long_dur = None, None
        try:
            long_video, long_dur = self._long_version(moment, long_segments or segments, streamer, out_dir, stream_live, (src_w, src_h), margin_v)
        except Exception:  # noqa: BLE001
            log.exception("versão longa falhou (clipe curto segue normal)")
        return RenderResult(video=video, cover=cover, duration=duration, words=words, encoder=encoder,
                            music_video=music_video, music=music, zoom_peaks=peaks, layout=layout, margin_v=margin_v,
                            long_video=long_video, long_duration=long_dur)

    def _long_version(self, moment: dict, segments: list[dict], streamer: Streamer, out_dir: Path, stream_live: bool,
                      src: tuple[int, int], margin_v: int) -> tuple[Path | None, float | None]:
        lv = self.e.get("long_version", {})
        if not lv.get("enabled", False):
            return None, None
        min_s, max_s = float(lv.get("min_seconds", 62)), float(lv.get("max_seconds", 95))
        target = float(lv.get("target_seconds", 75))
        if moment["end_ts"] - moment["start_ts"] >= min_s:
            return None, None  # o clipe curto já é longo o bastante
        window = {**moment, "end_ts": moment["start_ts"] + max_s}
        try:
            chosen, offset = source_for(window, segments, stream_live)
        except MediaNotReady as exc:
            log.info("versão longa adiada/pulada: %s", exc)
            return None, None
        avail = min(window["end_ts"], chosen[-1]["end_ts"]) - max(window["start_ts"], chosen[0]["start_ts"])
        if avail < min_s + 2:
            log.info("versão longa pulada: só %.0fs de mídia após o início", avail)
            return None, None
        concat = out_dir / "source_long.ffconcat"
        build_concat(chosen, concat)
        audio = decode_audio(Path(), start=offset, duration=avail, concat_list=concat)
        transcript = self.transcriber.transcribe(audio)
        from clipador.detect.scorer import refine_bounds

        peak_rel = max(0.5, moment["peak_ts"] - moment["start_ts"])
        start_rel, end_rel = refine_bounds(peak_rel, transcript, {"min_seconds": min_s, "max_seconds": min(max_s, avail),
                                                                  "target_seconds": target, "pre_roll_seconds": 1.5}, media_end_rel=avail)
        start_rel = max(0.0, min(start_rel, 3.0))   # mantém o mesmo gancho do clipe curto (no máximo 3 s de diferença)
        if end_rel - start_rel < min_s:
            end_rel = min(avail, start_rel + min_s + 1.0)
        dur = round(end_rel - start_rel, 2)
        words = [Word(w.start - start_rel, w.end - start_rel, w.text) for w in transcript.words if start_rel <= w.start and w.end <= end_rel + 0.3]
        subs = self.e.get("subtitles", {})
        ass_path = out_dir / "subs_long.ass"
        ass_path.write_text(build_ass(words, subs, int(self.e.get("width", 1080)), int(self.e.get("height", 1920)), margin_v), encoding="utf-8")
        peaks: list[float] = []
        if self.e.get("zoom", {}).get("enabled", True):
            sub_audio = audio[int(start_rel * 16000): int(end_rel * 16000)]
            peaks = pick_zoom_peaks(sub_audio, dur, peak_rel - start_rel, int(self.e.get("zoom", {}).get("max_per_clip", 3)) + 2,
                                    float(self.e.get("zoom", {}).get("min_gap_seconds", 4.0)))
        out = out_dir / "clip_long.mp4"
        self._render_video(concat, offset + start_rel, dur, ass_path, streamer, out, out_dir, src, peaks)
        real = _probe_duration(out) or dur
        if real < min_s - 1:
            log.warning("versão longa ficou com %.0fs (< %.0fs): mídia/áudio acabou antes; descartada", real, min_s)
            out.unlink(missing_ok=True)
            return None, None
        log.info("versão longa: %.0fs -> %s", real, out.name)
        return out, round(real, 1)

    # ------------------------------------------------------------------
    def _layout(self, streamer: Streamer, src: tuple[int, int]) -> dict:
        """Geometria do foreground: gameplay (e facecam) em px do canvas 1080x1920."""
        w, h = int(self.e.get("width", 1080)), int(self.e.get("height", 1920))
        src_w, src_h = src
        game_h = _even(w * src_h / src_w)
        if not streamer.facecam:
            return {"mode": "center", "game": (w, game_h, (h - game_h) // 2), "face": None}
        fc = streamer.facecam
        fx, fy, fw, fh = (float(fc.get(k, d)) for k, d in (("x", 0), ("y", 0), ("w", 0.25), ("h", 0.25)))
        fx, fy = min(max(fx, 0.0), 0.95), min(max(fy, 0.0), 0.95)
        fw, fh = min(max(fw, 0.05), 1.0 - fx), min(max(fh, 0.05), 1.0 - fy)
        crop_w, crop_h = _even(src_w * fw), _even(src_h * fh)
        crop_x, crop_y = int(src_w * fx) // 2 * 2, int(src_h * fy) // 2 * 2
        face_h = _even(w * crop_h / crop_w)
        max_face = int(self.e.get("facecam_max_height", 760))
        face_w = w
        if face_h > max_face:  # facecam muito alta (ex.: recorte vertical): reduz largura para caber
            face_w = _even(w * max_face / face_h)
            face_h = max_face
        total = face_h + game_h
        top = max(0, (h - total) // 2 - int(self.e.get("split_shift_up", 60)))
        return {"mode": "split", "game": (w, game_h, top + face_h), "face": (face_w, face_h, top, (crop_w, crop_h, crop_x, crop_y))}

    def _filter_graph(self, ass_path: Path, streamer: Streamer, src: tuple[int, int], peaks: list[float]) -> tuple[str, list[str]]:
        w, h = int(self.e.get("width", 1080)), int(self.e.get("height", 1920))
        fps = int(self.e.get("fps", 30))
        lay = self._layout(streamer, src)
        game_w, game_h, game_y = lay["game"]
        extra_inputs: list[str] = []
        parts: list[str] = []
        n_split = 3 if lay["face"] else 2
        labels = "[bg][fg]" + ("[fc]" if lay["face"] else "")
        if self.e.get("background", "blur") == "blur":
            parts.append(f"[0:v]fps={fps},split={n_split}{labels}")
            parts.append(f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:3,eq=brightness=-0.12:saturation=1.1[bgb]")
        else:
            parts.append(f"[0:v]fps={fps},split={n_split}{labels}")
            parts.append(f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},eq=brightness=-1[bgb]")
        zoom_cfg = self.e.get("zoom", {})
        if peaks:
            z = zoom_expression(peaks, fps, float(zoom_cfg.get("amount", 0.12)), float(zoom_cfg.get("width_seconds", 1.2)))
            parts.append(f"[fg]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={game_w}x{game_h}:fps={fps}[fgs]")
        else:
            parts.append(f"[fg]scale={game_w}:{game_h}:flags=lanczos[fgs]")
        parts.append(f"[bgb][fgs]overlay=(W-w)/2:{game_y}:shortest=1[v1]")
        last = "v1"
        if lay["face"]:
            face_w, face_h, face_y, (cw, ch, cx, cy) = lay["face"]
            parts.append(f"[fc]crop={cw}:{ch}:{cx}:{cy},scale={face_w}:{face_h}:flags=lanczos[fcs]")
            parts.append(f"[{last}][fcs]overlay=(W-w)/2:{face_y}[v1b]")
            last = "v1b"
        # legendas (arquivo relativo: ffmpeg roda com cwd=out_dir para evitar escape de caminho Windows)
        parts.append(f"[{last}]ass={ass_path.name}[v2]")
        last = "v2"
        credit = self.e.get("credit", {})
        if credit.get("enabled", True):
            text = _text_escape(credit.get("template", "Clipe: {display}").format(display=streamer.display, name=streamer.name))
            font = _ff_escape(credit.get("font_file", "C:/Windows/Fonts/arialbd.ttf"))
            size = int(credit.get("font_size", 34))
            parts.append(f"[{last}]drawtext=fontfile='{font}':text='{text}':fontsize={size}:fontcolor=white@0.85:"
                         f"borderw=2:bordercolor=black@0.6:x=(w-tw)/2:y=h-150[v3]")
            last = "v3"
        wm = self.e.get("watermark", {})
        wm_file = self.cfg.settings.resolve(wm.get("file", "assets/watermark/watermark.png")) if wm.get("enabled") else None
        if wm_file and wm_file.exists():
            extra_inputs += ["-i", str(wm_file)]
            width = int(wm.get("width", 220))
            opacity = float(wm.get("opacity", 0.6))
            x = "W-w-40" if wm.get("position", "top-right") == "top-right" else "40"
            # na tela dividida a facecam começa mais alto: sobe a marca d'água para a faixa livre
            y = 120 if lay["mode"] == "center" else max(24, lay["face"][2] - 90)
            parts.append(f"[1:v]scale={width}:-1,format=rgba,colorchannelmixer=aa={opacity}[wm]")
            parts.append(f"[{last}][wm]overlay={x}:{y}[v4]")
            last = "v4"
        parts.append(f"[{last}]format=yuv420p[vout]")
        parts.append(f"[0:a]loudnorm={self.e.get('loudnorm', 'I=-14:TP=-1.5:LRA=11')},aresample=48000[aout]")
        return ";".join(parts), extra_inputs

    def _render_video(self, concat: Path, offset: float, duration: float, ass_path: Path, streamer: Streamer,
                      out: Path, cwd: Path, src: tuple[int, int], peaks: list[float]) -> str:
        graph, extra_inputs = self._filter_graph(ass_path, streamer, src, peaks)
        encoders = [self.e.get("encoder", "h264_nvenc"), "libx264"]
        last_err = ""
        for enc in dict.fromkeys(encoders):
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-analyzeduration", "20M", "-probesize", "50M",
                   "-ss", f"{offset:.3f}", "-f", "concat", "-safe", "0", "-i", str(concat), *extra_inputs,
                   "-t", f"{duration:.3f}", "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]"]
            if enc == "h264_nvenc":
                cmd += ["-c:v", "h264_nvenc", "-preset", str(self.e.get("nvenc_preset", "p5")), "-rc", "vbr",
                        "-cq", "23", "-b:v", str(self.e.get("video_bitrate", "8M")),
                        "-maxrate", str(self.e.get("max_bitrate", "12M")), "-bufsize", "16M", "-profile:v", "high",
                        "-bf", "2", "-spatial-aq", "1", "-temporal-aq", "1"]
            else:
                cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-maxrate", str(self.e.get("max_bitrate", "12M")),
                        "-bufsize", "16M", "-profile:v", "high"]
            cmd += ["-g", str(int(self.e.get("fps", 30)) * 2), "-c:a", "aac", "-b:a", str(self.e.get("audio_bitrate", "192k")),
                    "-ar", "48000", "-movflags", "+faststart", "-shortest", str(out)]
            t0 = time.time()
            proc = subprocess.run(cmd, capture_output=True, cwd=str(cwd), timeout=900)
            if proc.returncode == 0 and out.exists() and out.stat().st_size > 10_000:
                log.info("renderizado com %s em %.1fs (zoom em %s) -> %s", enc, time.time() - t0, peaks or "nada", out)
                return enc
            last_err = proc.stderr.decode("utf-8", "replace")[-600:]
            log.warning("encoder %s falhou: %s", enc, last_err.strip()[:300])
        raise RuntimeError(f"render falhou com todos os encoders: {last_err}")

    # ------------------------------------------------------------------
    def _music_version(self, video: Path, out_dir: Path, moment: dict, duration: float) -> tuple[Path | None, dict | None]:
        m = self.e.get("music", {})
        if not m.get("enabled", True):
            return None, None
        music_dir = self.cfg.settings.resolve(m.get("dir", "assets/music_free"))
        tracks = list_music(music_dir)
        if not tracks:
            return None, None
        rng = random.Random(int(moment["id"]) * 7919)
        track = rng.choice(tracks)
        track_len = _probe_duration(track)
        start = round(rng.uniform(0, max(0.0, track_len - duration - 2)), 1) if track_len > duration + 5 else 0.0
        lufs = float(m.get("target_lufs", -27))
        vol_db = float(m.get("volume_db", 0))
        out = out_dir / "clip_youtube.mp4"
        # música normalizada por loudness (independe do volume do arquivo), depois ducking pela voz
        graph = (f"[0:a]asplit=2[voice][sc];"
                 f"[1:a]atrim=start={start:.1f},asetpts=PTS-STARTPTS,aresample=48000,"
                 f"loudnorm=I={lufs:.0f}:TP=-6:LRA=9,volume={vol_db:.1f}dB[mus];"
                 f"[mus][sc]sidechaincompress=threshold={float(m.get('duck_threshold', 0.03)):.3f}:ratio={float(m.get('duck_ratio', 6)):.1f}"
                 f":attack=40:release=600:makeup=1[duck];"
                 f"[voice][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin", "-i", str(video), "-stream_loop", "-1", "-i", str(track),
               "-filter_complex", graph, "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
               "-b:a", str(self.e.get("audio_bitrate", "192k")), "-ar", "48000", "-shortest", "-movflags", "+faststart", str(out)]
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0 or not out.exists():
            log.warning("mix de música falhou (%s): %s", track.name, proc.stderr.decode("utf-8", "replace")[-300:])
            return None, None
        lic = read_music_licenses(music_dir).get(track.name, {})
        info = {"file": track.name, "start": start, "target_lufs": lufs, **lic}
        log.info("versão YouTube com música %s (@%.0fs) em %.1fs", track.name, start, time.time() - t0)
        return out, info
