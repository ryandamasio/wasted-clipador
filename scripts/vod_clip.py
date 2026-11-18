"""Corte de VOD escolhido à mão: acha a frase na transcrição do VOD, baixa só o trecho em 1080p e põe na fila.

A transcrição vem de scripts/scan.py --audio (arquivo .words.json com tempos relativos ao trecho baixado);
--words-offset diz em que ponto do VOD esse trecho começa.

  .venv/Scripts/python scripts/vod_clip.py --streamer alanzoka --vod 2932200023 \
      --words data/backfill/alanzoka_0030-0130.words.json --words-offset 0:30:00 \
      --at 0:35:15 --phrase "olha essa intro" --pre 45 --score 92 --title "..." --note "..."
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipador.capture.monitor import ytdlp_cmd  # noqa: E402
from clipador.config import load_config  # noqa: E402
from clipador.detect.content import analyze  # noqa: E402
from clipador.detect.scorer import refine_bounds  # noqa: E402
from clipador.detect.transcribe import Transcript, Word  # noqa: E402
from clipador.store.db import Database  # noqa: E402
from scripts.curate import find_phrase  # noqa: E402


def hms(s: str) -> float:
    parts = [float(p) for p in s.split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--streamer", required=True)
    ap.add_argument("--vod", required=True, help="id do VOD da Twitch")
    ap.add_argument("--words", required=True)
    ap.add_argument("--words-offset", default="0:00:00")
    ap.add_argument("--at", required=True, help="posição no VOD (H:MM:SS) perto da frase")
    ap.add_argument("--phrase", required=True)
    ap.add_argument("--pre", type=float, default=6, help="segundos antes da frase (contexto, ex.: contagem regressiva)")
    ap.add_argument("--max", type=float, default=95)
    ap.add_argument("--score", type=float, required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--note", required=True)
    ap.add_argument("--facecam", help="x,y,w,h da câmera nesse trecho (frações) ou none")
    ap.add_argument("--quality", default="1080p60/720p60/best")
    ap.add_argument("--vod-start", default=time.strftime("%Y-%m-%d") + " 07:25:00", help="hora real em que o VOD começou")
    args = ap.parse_args()

    cfg = load_config()
    db = Database(cfg.settings.path("db_path"))
    off = hms(args.words_offset)
    words = [(w[0] + off, w[1] + off, w[2], 0, 0) for w in json.loads(Path(args.words).read_text(encoding="utf-8"))]
    i = find_phrase(words, args.phrase, hms(args.at), radius=150)
    if i is None:
        print(f"frase '{args.phrase}' não encontrada perto de {args.at}"); return 2
    peak = words[i][0]
    base = peak - 120
    rel = [Word(w[0] - base, w[1] - base, w[2]) for w in words if base <= w[0] <= peak + 150]
    clip_cfg = {**cfg.settings.get("detect.clip", {}), "pre_roll_seconds": args.pre, "max_seconds": args.max}
    s_rel, e_rel = refine_bounds(120.0, Transcript(words=rel, text=""), clip_cfg)
    start, end = base + s_rel, base + e_rel  # posições no VOD

    # baixa só o trecho (com folga de 3 s) na melhor qualidade
    url = subprocess.run([*ytdlp_cmd(), "--no-warnings", "-g", "-f", args.quality, f"https://www.twitch.tv/videos/{args.vod}"],
                         capture_output=True, text=True).stdout.strip().splitlines()[-1]
    out_dir = cfg.settings.path("recordings_dir") / f"{args.streamer}_vod{args.vod}"
    out_dir.mkdir(parents=True, exist_ok=True)
    a, b = max(0.0, start - 3), end + 3
    seg_path = out_dir / f"vod_{int(a)}_{int(b)}.ts"
    if not seg_path.exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{a:.2f}", "-i", url, "-t", f"{b - a:.2f}",
                        "-c", "copy", "-f", "mpegts", str(seg_path)], check=True)

    vod_epoch = datetime.strptime(args.vod_start, "%Y-%m-%d %H:%M:%S").timestamp()
    stream_id = db.create_stream(args.streamer, "twitch", f"https://www.twitch.tv/videos/{args.vod}", out_dir, title=f"VOD {args.vod} (GTA 6)")
    db.end_stream(stream_id, "ended")
    seg_id = db.add_segment(stream_id, 0, seg_path, vod_epoch + a, vod_epoch + b)
    db.update("segments", seg_id, status="detected")
    inside = [w for w in words if start <= w[0] <= end]
    text = " ".join(w[2] for w in inside)
    reasons = {"notes": [f"curadoria (VOD): {args.note}"], "curated": True, "content": analyze(text), "chat": None, "audio": 0,
               "transcript": 0, "keywords": 0, "keywords_found": [], "title": args.title}
    if args.facecam:
        reasons["facecam"] = None if args.facecam == "none" else dict(zip("xywh", (float(v) for v in args.facecam.split(","))))
    mid = db.add_moment(stream_id, seg_id, vod_epoch + start, vod_epoch + end, vod_epoch + peak, args.score, reasons, text,
                        [[round(vod_epoch + w[0], 3), round(vod_epoch + w[1], 3), w[2]] for w in inside])
    v = lambda t: f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"
    print(f"momento {mid}: VOD {v(start)}-{v(end)} ({end - start:.0f}s) nota {args.score:.0f} | {args.title}")
    print("   " + text[:260])
    return 0


if __name__ == "__main__":
    sys.exit(main())
