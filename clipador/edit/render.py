"""Renderização do clipe vertical 9:16 com ffmpeg (NVENC, fallback libx264).

Entrada: momento (timestamps absolutos) + segmentos que cobrem o intervalo.
Saída: clip.mp4, cover.jpg, subs.ass em um diretório do clipe.
"""
from __future__ import annotations

import logging
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from clipador.config import Config, Streamer
from clipador.detect.audio import decode_audio
from clipador.detect.transcribe import Transcriber, Word
from clipador.edit.subtitles import build_ass

log = logging.getLogger("edit.render")


class MediaNotReady(Exception):
    """Os segmentos ainda não cobrem o fim do clipe (live em andamento)."""


@dataclass
class RenderResult:
    video: Path
    cover: Path | None
    duration: float
    words: list[Word]
    encoder: str


def _ff_escape(path: Path | str) -> str:
    """Escapa caminho para uso dentro de filter_complex (Windows: C\\:/dir/file)."""
    s = str(path).replace("\\", "/")
    s = s.replace(":", "\\:")
    return s.replace("'", "\\'")


def _text_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


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


class Renderer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.e = cfg.settings.section("edit")
        self.transcriber = Transcriber.get(cfg.settings.get("detect.whisper", {}))

    def render(self, moment: dict, segments: list[dict], streamer: Streamer, out_dir: Path, stream_live: bool) -> RenderResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        chosen, offset = source_for(moment, segments, stream_live)
        duration = min(moment["end_ts"], chosen[-1]["end_ts"]) - max(moment["start_ts"], chosen[0]["start_ts"])
        duration = max(5.0, round(duration, 2))
        concat = out_dir / "source.ffconcat"
        build_concat(chosen, concat)

        # 1) transcrição precisa do próprio clipe (timestamps relativos ao clipe)
        t0 = time.time()
        audio = decode_audio(Path(), start=offset, duration=duration, concat_list=concat)
        transcript = self.transcriber.transcribe(audio)
        words = [w for w in transcript.words if w.end <= duration + 0.5]
        log.info("clipe transcrito em %.1fs (%d palavras)", time.time() - t0, len(words))

        # 2) legenda ASS
        subs = self.e.get("subtitles", {})
        margin_v = random.choice(subs.get("margin_v_options", [560]))
        ass_path = out_dir / "subs.ass"
        ass_path.write_text(build_ass(words, subs, int(self.e.get("width", 1080)), int(self.e.get("height", 1920)), margin_v),
                            encoding="utf-8")

        # 3) render
        video = out_dir / "clip.mp4"
        encoder = self._render_video(concat, offset, duration, ass_path, streamer, video, out_dir)

        # 4) capa no pico
        cover = out_dir / "cover.jpg"
        peak_rel = max(0.5, min(duration - 0.5, moment["peak_ts"] - moment["start_ts"]))
        try:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{peak_rel:.2f}", "-i", str(video),
                            "-frames:v", "1", "-q:v", "3", str(cover)], check=True, capture_output=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log.warning("capa falhou: %s", exc)
            cover = None
        return RenderResult(video=video, cover=cover, duration=duration, words=words, encoder=encoder)

    # ------------------------------------------------------------------
    def _filter_graph(self, ass_path: Path, streamer: Streamer, out_dir: Path) -> tuple[str, list[str]]:
        w, h = int(self.e.get("width", 1080)), int(self.e.get("height", 1920))
        fps = int(self.e.get("fps", 30))
        extra_inputs: list[str] = []
        parts: list[str] = []
        if self.e.get("background", "blur") == "blur":
            parts.append(f"[0:v]fps={fps},split=2[bg][fg]")
            parts.append(f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:3,eq=brightness=-0.12:saturation=1.1[bgb]")
        else:
            parts.append(f"[0:v]fps={fps},null[fg]")
            parts.append(f"color=c=black:s={w}x{h}:r={fps}[bgb]")
        parts.append(f"[fg]scale={w}:-2:flags=lanczos[fgs]")
        parts.append("[bgb][fgs]overlay=(W-w)/2:(H-h)/2:shortest=1[v1]")
        # legendas (arquivo relativo: ffmpeg roda com cwd=out_dir para evitar escape de caminho Windows)
        parts.append(f"[v1]ass={ass_path.name}[v2]")
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
            parts.append(f"[1:v]scale={width}:-1,format=rgba,colorchannelmixer=aa={opacity}[wm]")
            parts.append(f"[{last}][wm]overlay={x}:120[v4]")
            last = "v4"
        parts.append(f"[{last}]format=yuv420p[vout]")
        parts.append(f"[0:a]loudnorm={self.e.get('loudnorm', 'I=-14:TP=-1.5:LRA=11')},aresample=48000[aout]")
        return ";".join(parts), extra_inputs

    def _render_video(self, concat: Path, offset: float, duration: float, ass_path: Path, streamer: Streamer,
                      out: Path, cwd: Path) -> str:
        graph, extra_inputs = self._filter_graph(ass_path, streamer, cwd)
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
                log.info("renderizado com %s em %.1fs -> %s", enc, time.time() - t0, out)
                return enc
            last_err = proc.stderr.decode("utf-8", "replace")[-600:]
            log.warning("encoder %s falhou: %s", enc, last_err.strip()[:300])
        raise RuntimeError(f"render falhou com todos os encoders: {last_err}")
