"""CLI do Clipador.

  python -m clipador run                      # serviço completo (monitor + detecção + edição)
  python -m clipador check                    # quem está online agora
  python -m clipador process-file VIDEO --streamer HANDLE [--chat CHAT.jsonl]
                                              # roda detecção+edição num vídeo local (teste/VOD)
  python -m clipador record URL --streamer HANDLE --minutes 10
                                              # grava uma live por N minutos e processa (teste real)
  python -m clipador status                   # contadores do banco e últimos clipes
  python -m clipador healthcheck              # ffmpeg, NVENC, CUDA, navegador, disco
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from clipador.config import load_config
from clipador.logging_setup import setup_logging
from clipador.store.db import Database

log = logging.getLogger("cli")


def _db(cfg) -> Database:
    return Database(cfg.settings.path("db_path"))


def cmd_run(args, cfg) -> int:
    from clipador.orchestrator import Orchestrator

    Orchestrator(cfg, _db(cfg)).run()
    return 0


def cmd_check(args, cfg) -> int:
    from clipador.capture.monitor import check_online, find_browser

    browser = find_browser(cfg.settings.get("capture.browser_executable", "auto"))
    streamers = cfg.streamers if args.all else cfg.enabled_streamers
    for s in streamers:
        t0 = time.time()
        try:
            online, title = check_online(s.url, s.platform, browser)
        except Exception as exc:  # noqa: BLE001
            online, title = False, f"erro: {str(exc)[:100]}"
        print(f"{'ONLINE ' if online else 'offline'}  {s.platform:7s} {s.handle:18s} {title or ''}  ({time.time() - t0:.1f}s)")
    return 0


def _probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def cmd_process_file(args, cfg) -> int:
    """Simula a live: divide o vídeo em segmentos, roda detecção e edição e escreve clipes em data/output."""
    from clipador.orchestrator import Orchestrator

    streamer = cfg.streamer(args.streamer)
    if not streamer:
        print(f"streamer '{args.streamer}' não está em config/streamers.yaml", file=sys.stderr)
        return 2
    src = Path(args.video).resolve()
    if not src.exists():
        print(f"arquivo não encontrado: {src}", file=sys.stderr)
        return 2
    db = _db(cfg)
    orch = Orchestrator(cfg, db)
    seg_len = int(cfg.settings.get("capture.segment_seconds", 120))
    total = _probe_duration(src)
    base_ts = args.base_ts if args.base_ts else time.time() - total
    run_dir = cfg.settings.path("recordings_dir") / f"{streamer.handle}_file_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    stream_id = db.create_stream(streamer.handle, streamer.platform, str(src), run_dir, title=src.stem)
    db.end_stream(stream_id, "ended")

    log.info("dividindo %s (%.0fs) em segmentos de %ds...", src.name, total, seg_len)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy", "-f", "segment", "-segment_time", str(seg_len), "-segment_format", "mpegts",
                    "-reset_timestamps", "1", "-segment_list", str(run_dir / "segments.csv"), "-segment_list_type", "csv",
                    str(run_dir / "seg_%05d.ts")], check=True)
    seg_ids = []
    for line in (run_dir / "segments.csv").read_text().splitlines():
        fname, s, e = line.split(",")[:3]
        seg_ids.append(db.add_segment(stream_id, len(seg_ids), run_dir / fname, base_ts + float(s), base_ts + float(e)))

    if args.chat:
        rows = []
        for ln in Path(args.chat).read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            m = json.loads(ln)
            # chat de arquivo: "t" em segundos relativos ao vídeo (já alinhado), "user", "text"
            rows.append((stream_id, base_ts + float(m["t"]) - cfg.settings.get("capture.chat_offset_seconds", 10), m.get("user"), m["text"]))
        db.add_chat_messages(rows)
        log.info("%d mensagens de chat carregadas", len(rows))

    t0 = time.time()
    for sid in seg_ids:
        orch.detect_one(db.get_segment(sid))
    log.info("detecção concluída em %.1fs", time.time() - t0)
    moments = db.pending_moments()
    if args.max_clips:
        moments = moments[: args.max_clips]
    log.info("%d momento(s) para renderizar", len(moments))
    for m in moments:
        orch.process_moment(m)
    print()
    print(f"Clipes em: {cfg.settings.path('output_dir')}")
    for c in db.fetchall("SELECT c.* FROM clips c JOIN moments m ON m.id=c.moment_id WHERE m.stream_id=? ORDER BY m.score DESC", [stream_id]):
        print(f"  [{json.loads(c['copy_json'])['titles'][0]}]  {c['path']}")
    return 0


def cmd_record(args, cfg) -> int:
    """Grava uma live por N minutos (com chat) e processa — teste real ponta a ponta."""
    from clipador.capture.chat import make_chat_recorder
    from clipador.capture.monitor import find_browser
    from clipador.capture.recorder import Recorder
    from clipador.orchestrator import Orchestrator

    streamer = cfg.streamer(args.streamer)
    if not streamer:
        print(f"streamer '{args.streamer}' não está em config/streamers.yaml", file=sys.stderr)
        return 2
    if args.url:
        streamer.url = args.url
    db = _db(cfg)
    orch = Orchestrator(cfg, db)
    cap = cfg.settings.section("capture")
    browser = find_browser(cap.get("browser_executable", "auto"))
    rec = Recorder(db, streamer, cap, cfg.settings.path("recordings_dir"), browser)
    chat = make_chat_recorder(db, rec.stream_id, streamer)
    chat.start()
    rec.start()
    deadline = time.time() + args.minutes * 60
    try:
        while time.time() < deadline and rec.is_alive():
            seg = db.next_segment_to_detect()
            if seg:
                orch.detect_one(seg)
                for m in db.pending_moments():
                    orch.process_moment(m)
            else:
                time.sleep(3)
    except KeyboardInterrupt:
        log.info("interrompido")
    finally:
        rec.stop()
        chat.stop()
        rec.join(timeout=20)
    db.end_stream(rec.stream_id, "ended")
    # processa o que sobrou
    while (seg := db.next_segment_to_detect()):
        orch.detect_one(seg)
    for m in db.pending_moments():
        orch.process_moment(m)
    log.info("chat: %d mensagens gravadas", db.chat_count(rec.stream_id))
    print(f"\nClipes em: {cfg.settings.path('output_dir')}")
    return 0


def cmd_status(args, cfg) -> int:
    db = _db(cfg)
    print("Tabelas:", json.dumps(db.stats()))
    print("\nStreams ao vivo:", [s["streamer"] for s in db.live_streams()] or "nenhuma")
    print("\nÚltimos momentos:")
    for m in db.moments(10):
        r = json.loads(m["reasons"])
        print(f"  #{m['id']:4d} nota {m['score']:5.1f} {m['status']:14s} {time.strftime('%d/%m %H:%M:%S', time.localtime(m['start_ts']))} "
              f"{m['end_ts'] - m['start_ts']:4.0f}s  {'; '.join(r.get('notes', []))}")
    print("\nÚltimos clipes:")
    for c in db.clips(limit=10):
        print(f"  #{c['id']:4d} {c['status']:9s} {c['title'][:50]:50s} {c['path']}")
    return 0


def cmd_healthcheck(args, cfg) -> int:
    from clipador.capture.monitor import find_browser
    from clipador.capture.recorder import free_space_gb

    ok = True

    def item(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'OK' if good else 'FALHA'}] {name} {detail}")

    item("ffmpeg", shutil.which("ffmpeg"), shutil.which("ffmpeg") or "")
    enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    item("h264_nvenc", "h264_nvenc" in enc)
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        item("CUDA (ctranslate2)", n > 0, f"{n} GPU(s)")
    except Exception as exc:  # noqa: BLE001
        item("CUDA (ctranslate2)", False, str(exc)[:80])
    try:
        smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                             capture_output=True, text=True).stdout.strip()
        item("nvidia-smi", bool(smi), smi)
    except FileNotFoundError:
        item("nvidia-smi", False, "não encontrado")
    item("streamlink", True, subprocess.run([sys.executable, "-m", "streamlink", "--version"], capture_output=True, text=True).stdout.strip())
    b = find_browser(cfg.settings.get("capture.browser_executable", "auto"))
    item("navegador p/ Kick", b, b or "Chrome/Edge não encontrado (Kick pode falhar)")
    free = free_space_gb(cfg.settings.path("data_dir"))
    item("disco", free > cfg.settings.get("capture.min_free_gb", 20), f"{free:.0f} GB livres em {cfg.settings.path('data_dir').drive}")
    font = Path(cfg.settings.get("edit.credit.font_file", ""))
    item("fonte do crédito", font.exists(), str(font))
    wm = cfg.settings.resolve(cfg.settings.get("edit.watermark.file"))
    print(f"  [{'OK' if wm.exists() else 'AVISO'}] marca d'água {'encontrada' if wm.exists() else 'ausente (opcional): ' + str(wm)}")
    print(f"  [INFO] LLM: {'ativo' if cfg.settings.get('detect.llm.enabled') else 'desligado (local apenas)'}")
    if args.whisper:
        from clipador.detect.transcribe import Transcriber
        import numpy as np
        t = Transcriber.get(cfg.settings.get("detect.whisper", {}))
        t0 = time.time()
        t.transcribe(np.zeros(16000 * 5, dtype=np.float32))
        item("Whisper", True, f"{t.device}/{t.compute_type} carregado em {time.time() - t0:.1f}s")
    return 0 if ok else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="clipador", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    c = sub.add_parser("check")
    c.add_argument("--all", action="store_true", help="inclui streamers desabilitados")
    pf = sub.add_parser("process-file")
    pf.add_argument("video")
    pf.add_argument("--streamer", required=True)
    pf.add_argument("--chat", help="JSONL com {t, user, text} (t em segundos do vídeo)")
    pf.add_argument("--max-clips", type=int, default=0)
    pf.add_argument("--base-ts", type=float, default=0.0)
    r = sub.add_parser("record")
    r.add_argument("--streamer", required=True)
    r.add_argument("--url", help="sobrescreve a URL do streamers.yaml")
    r.add_argument("--minutes", type=float, default=10)
    sub.add_parser("status")
    h = sub.add_parser("healthcheck")
    h.add_argument("--whisper", action="store_true", help="também carrega o Whisper na GPU")
    args = p.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.settings.path("logs_dir"), logging.DEBUG if args.verbose else logging.INFO)
    return {"run": cmd_run, "check": cmd_check, "process-file": cmd_process_file, "record": cmd_record,
            "status": cmd_status, "healthcheck": cmd_healthcheck}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
