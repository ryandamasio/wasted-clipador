"""Calibração offline da detecção.

Recalcula as notas por segundo de uma stream já gravada usando as transcrições em cache (.transcript.json),
o chat do banco e o áudio dos segmentos, e mostra a distribuição das notas e os picos para diferentes
pesos/limiares, sem rodar o Whisper de novo.

Uso:
  .venv/Scripts/python scripts/calibrate.py --stream 2
  .venv/Scripts/python scripts/calibrate.py --stream 2 --weights chat=0.5,audio=0.2,transcript=0.15,keywords=0.15 --offset 8
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipador.config import load_config  # noqa: E402
from clipador.detect import signals as sig  # noqa: E402
from clipador.detect.audio import decode_audio, per_second_energy_signal  # noqa: E402
from clipador.detect.scorer import combine, pick_peaks  # noqa: E402
from clipador.detect.transcribe import Transcript  # noqa: E402
from clipador.store.db import Database  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", type=int, required=True)
    ap.add_argument("--weights", help="ex.: chat=0.45,audio=0.25,transcript=0.15,keywords=0.15")
    ap.add_argument("--offset", type=float, help="chat_offset_seconds a testar")
    ap.add_argument("--min-score", type=float)
    ap.add_argument("--top", type=int, default=8, help="quantos picos mostrar")
    args = ap.parse_args()

    cfg = load_config()
    d = cfg.settings.section("detect")
    weights = dict(d["weights"])
    if args.weights:
        weights = {k: float(v) for k, v in (p.split("=") for p in args.weights.split(","))}
    offset = args.offset if args.offset is not None else float(cfg.settings.get("capture.chat_offset_seconds", 10))
    min_score = args.min_score if args.min_score is not None else float(d["min_score"])
    db = Database(cfg.settings.path("db_path"))
    stream = db.get_stream(args.stream)
    if not stream:
        print("stream não encontrada"); return 2
    segs = db.fetchall("SELECT * FROM segments WHERE stream_id=? AND status!='deleted' ORDER BY idx", [args.stream])
    print(f"stream {args.stream} ({stream['streamer']}): {len(segs)} segmentos, {db.chat_count(args.stream)} msgs de chat, "
          f"offset={offset}s, pesos={weights}, min_score={min_score}")

    all_scores: list[np.ndarray] = []
    baseline = None
    rows = []
    for s in segs:
        path = Path(s["path"])
        tpath = path.with_suffix(".transcript.json")
        if not path.exists() or not tpath.exists():
            print(f"  seg {s['idx']}: sem mídia/transcrição em cache, pulando"); continue
        transcript = Transcript.from_json(json.loads(tpath.read_text(encoding="utf-8")))
        total = int(round(s["duration"]))
        audio = decode_audio(path)
        if len(audio) == 0:
            continue
        audio_sig, baseline = per_second_energy_signal(audio, total, 0.5, baseline)
        seg_chat_start = s["start_ts"] - offset
        msgs = db.chat_between(args.stream, seg_chat_start, seg_chat_start + total)
        chat_sig = None
        if msgs:
            prev = db.chat_between(args.stream, seg_chat_start - 600, seg_chat_start)
            if len(prev) >= 10:
                bins = np.zeros(600)
                for m in prev:
                    i = int(m["ts"] - (seg_chat_start - 600))
                    if 0 <= i < 600: bins[i] += 1
                sm = np.convolve(bins, np.ones(5) / 5, mode="same"); base, std = float(sm.mean()), float(sm.std())
            else:
                base = len(msgs) / max(total, 1); std = base * 0.6
            chat_sig, _ = sig.chat_signal(msgs, seg_chat_start, total, base, std, d["chat"]["hype_tokens"], float(d["chat"].get("min_burst_rate", 2.0)))
        trans_sig = sig.transcript_signal(transcript, total)
        kw_sig, kws = sig.keyword_signal(transcript, d.get("keywords", []), total)
        score, eff = combine({"chat": chat_sig, "audio": audio_sig, "transcript": trans_sig, "keywords": kw_sig}, weights, int(d["window_seconds"]))
        if sig.blocklist_hits(transcript, msgs, d.get("blocklist_words", [])):
            score = score * 0.4
        if sig.blocklist_hits(transcript, msgs, d.get("sensitive_words", [])):
            score = score * 0.7
        all_scores.append(score)
        for p in pick_peaks(score, 0, int(d["min_gap_seconds"]), args.top):
            lo, hi = max(0, p - 3), min(total, p + 6)
            text = " ".join(w.text for w in transcript.words_between(lo, hi))[:90]
            chat_txt = " | ".join(m["text"][:20] for m in msgs if seg_chat_start + p - 2 <= m["ts"] <= seg_chat_start + p + 4)[:80]
            rows.append((float(score[p]), s["idx"], p, float(chat_sig[p]) if chat_sig is not None else -1, float(audio_sig[p]),
                         float(trans_sig[p]), float(kw_sig[p]), text, chat_txt))
        print(f"  seg {s['idx']}: {len(msgs):4d} msgs | nota máx {score.max():5.1f} média {score.mean():5.1f} | pesos efetivos {eff}")

    if not all_scores:
        return 1
    allv = np.concatenate(all_scores)
    pct = np.percentile(allv, [50, 75, 90, 95, 98, 99])
    print("\nPercentis das notas por segundo: p50=%.0f p75=%.0f p90=%.0f p95=%.0f p98=%.0f p99=%.0f" % tuple(pct))
    print(f"segundos >= {min_score:.0f}: {(allv >= min_score).sum()} de {len(allv)} ({100 * (allv >= min_score).mean():.1f}%)")
    print(f"\nTop {args.top} picos (nota | seg | s | chat audio fala kw | fala... | chat...):")
    for r in sorted(rows, reverse=True)[: args.top]:
        print(f"  {r[0]:5.1f} | seg {r[1]} @{r[2]:4d}s | {r[3]:4.2f} {r[4]:4.2f} {r[5]:4.2f} {r[6]:4.2f} | {r[7]} | {r[8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
