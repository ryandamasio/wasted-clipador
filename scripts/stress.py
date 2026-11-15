"""Teste de carga: N "lives" simultâneas a partir de gravações locais, com detecção e edição reais.

Monta fontes de ~20 min concatenando segmentos .ts já gravados (ou usa os arquivos passados), reproduz cada
uma em tempo real como se fosse uma live (Recorder com url file:), roda os loops de detecção e edição do
orquestrador e mede: segmentos gravados x detectados (atraso da fila do Whisper), momentos, clipes, latência
momento -> clipe e uso de GPU. Usa banco e pastas separados (data/stress_*), sem tocar nos dados reais.

Uso:
  .venv/Scripts/python scripts/stress.py --minutes 8 --lives 3
  .venv/Scripts/python scripts/stress.py --minutes 8 --sources a.ts b.mp4 c.mp4 --min-score 40
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipador.capture.recorder import Recorder  # noqa: E402
from clipador.config import Streamer, load_config  # noqa: E402
from clipador.logging_setup import setup_logging  # noqa: E402
from clipador.orchestrator import Orchestrator  # noqa: E402
from clipador.store.db import Database  # noqa: E402


def build_sources(cfg, n: int, minutes: int, work: Path, real_recordings: Path) -> list[Path]:
    """Concatena segmentos .ts das gravações existentes (uma gravação por live falsa)."""
    runs = sorted({p.parent for p in real_recordings.glob("*/run_*/seg_*.ts")}, key=lambda d: -len(list(d.glob("seg_*.ts"))))
    out: list[Path] = []
    need = max(2, minutes * 60 // int(cfg.settings.get("capture.segment_seconds", 120)) + 1)
    for i, run in enumerate(runs[:n]):
        segs = sorted(run.glob("seg_*.ts"))[:need]
        lst = work / f"src{i}.txt"
        lst.write_text("".join(f"file '{s.resolve().as_posix()}'\n" for s in segs), encoding="utf-8")
        dst = work / f"src{i}.ts"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dst)], check=True)
        out.append(dst)
    while len(out) < n and out:
        out.append(out[len(out) % len(out)])
    return out


def gpu() -> str:
    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=8)
    ap.add_argument("--lives", type=int, default=3)
    ap.add_argument("--sources", nargs="*")
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--keep", action="store_true", help="não apaga data/stress_* no fim")
    args = ap.parse_args()

    cfg = load_config()
    paths = cfg.settings._data["paths"]
    real_recordings = cfg.settings.path("recordings_dir")
    paths["recordings_dir"] = "data/stress_recordings"
    paths["output_dir"] = "data/stress_output"
    paths["db_path"] = "data/stress.sqlite"
    cfg.settings._data["review"]["enabled"] = False
    cfg.settings._data["capture"]["record_chat"] = False
    cfg.settings._data["detect"]["max_moments_per_hour"] = 0
    if args.min_score is not None:
        cfg.settings._data["detect"]["min_score"] = args.min_score
        cfg.settings._data["detect"]["min_score_no_chat"] = args.min_score
    for key in ("recordings_dir", "output_dir"):
        shutil.rmtree(cfg.settings.path(key), ignore_errors=True)
        cfg.settings.path(key).mkdir(parents=True, exist_ok=True)
    Path(paths["db_path"]).unlink(missing_ok=True)
    setup_logging(cfg.settings.path("logs_dir"))
    work = cfg.settings.path("recordings_dir") / "_sources"
    work.mkdir(parents=True, exist_ok=True)
    sources = [Path(s).resolve() for s in args.sources] if args.sources else build_sources(cfg, args.lives, args.minutes, work, real_recordings)
    if not sources:
        print("sem gravações para montar as fontes; passe --sources"); return 2
    print(f"{len(sources)} live(s) falsa(s) por {args.minutes} min; GPU antes: {gpu()}")

    db = Database(cfg.settings.path("db_path"))
    orch = Orchestrator(cfg, db)
    recs: list[Recorder] = []
    for i, src in enumerate(sources):
        s = Streamer(name=f"Fake{i}", handle=f"fake{i}", platform="twitch", url="file:///" + src.as_posix())
        cfg.streamers.append(s)
        r = Recorder(db, s, cfg.settings.section("capture"), cfg.settings.path("recordings_dir"), None, title="stress")
        r.start(); recs.append(r)
    threads = [threading.Thread(target=orch._guard(orch.detect_loop), name="detect", daemon=True),
               threading.Thread(target=orch._guard(orch.edit_loop), name="edit", daemon=True)]
    for t in threads:
        t.start()
    t0 = time.time()
    max_backlog, samples = 0, []
    try:
        while time.time() - t0 < args.minutes * 60:
            time.sleep(15)
            backlog = db.fetchone("SELECT SUM(status='recorded') AS n FROM segments")["n"] or 0
            max_backlog = max(max_backlog, int(backlog))
            samples.append(gpu())
            done = db.fetchone("SELECT COUNT(*) AS n FROM segments WHERE status IN ('detected','failed')")["n"]
            print(f"  t={time.time() - t0:5.0f}s segs gravados={db.stats()['segments']:3d} detectados={done:3d} fila={backlog:2d} "
                  f"momentos={db.stats()['moments']:2d} clipes={db.stats()['clips']:2d} gpu={samples[-1]}")
    except KeyboardInterrupt:
        pass
    finally:
        for r in recs:
            r.stop()
        for r in recs:
            r.join(timeout=20)
        orch.stop_event.set()
    # deixa o que sobrou terminar (até 3 min)
    deadline = time.time() + 180
    while time.time() < deadline and (db.next_segment_to_detect() or db.pending_moments()):
        time.sleep(5)
    st = db.stats()
    lat = db.fetchone("""SELECT ROUND(AVG(c.created_at - m.peak_ts)/60.0,1) AS avg_min, ROUND(MAX(c.created_at - m.peak_ts)/60.0,1) AS max_min, COUNT(*) AS n
                         FROM clips c JOIN moments m ON m.id=c.moment_id""") or {}
    det = db.fetchall("SELECT status, COUNT(*) AS n FROM segments GROUP BY status")
    print("\nRESULTADO")
    print(f"  lives: {len(sources)} | duração: {args.minutes} min | segmentos: {st['segments']} {[(d['status'], d['n']) for d in det]}")
    print(f"  pior fila do Whisper: {max_backlog} segmento(s) esperando (0-1 = acompanha; >3 = atrasando)")
    print(f"  momentos: {st['moments']} | clipes: {st['clips']} | latência momento->clipe média {lat.get('avg_min')} min, máx {lat.get('max_min')} min")
    print(f"  GPU (util%, MB) amostras: {samples[::max(1, len(samples) // 8)]}")
    if not args.keep:
        db.close()
        for key in ("recordings_dir", "output_dir"):
            shutil.rmtree(cfg.settings.path(key), ignore_errors=True)
        Path(paths["db_path"]).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
