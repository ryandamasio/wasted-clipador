"""Corte escolhido à mão: acha a frase-chave na transcrição perto do horário e cria o momento (o serviço renderiza).

Uso:
  .venv/Scripts/python scripts/curate.py --streamer alanzoka --at 15:15:40 --phrase "jacaré" --score 90 \
      --note "deu o corpo pro jacaré comer"
O corte usa as regras normais (>= 1 min, começa em início de frase uns segundos antes, termina em pausa).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipador.config import load_config  # noqa: E402
from clipador.detect.content import analyze  # noqa: E402
from clipador.detect.scorer import refine_bounds  # noqa: E402
from clipador.detect.signals import _norm  # noqa: E402
from clipador.detect.transcribe import Transcript, Word  # noqa: E402
from clipador.store.db import Database  # noqa: E402


def streamer_words(db: Database, handle: str, since: float) -> list[tuple[float, float, str, int, int]]:
    """(início, fim, palavra, stream_id, segment_id) de todas as gravações do streamer desde `since`."""
    out = []
    for s in db.fetchall("""SELECT g.* FROM segments g JOIN streams s ON s.id=g.stream_id
                            WHERE s.streamer=? AND g.start_ts>=? AND g.status!='deleted' ORDER BY g.start_ts""", [handle, since]):
        tp = Path(s["path"]).with_suffix(".transcript.json")
        if tp.exists() and Path(s["path"]).exists():
            t = Transcript.from_json(json.loads(tp.read_text(encoding="utf-8")))
            out += [(s["start_ts"] + w.start, s["start_ts"] + w.end, w.text, s["stream_id"], s["id"]) for w in t.words]
    return sorted(out)


def find_phrase(words, phrase: str, at: float, radius: float = 120) -> int | None:
    """Índice da primeira palavra da frase mais próxima de `at`."""
    target = _norm(phrase).split()
    best, best_d = None, 1e9
    for i in range(len(words)):
        if abs(words[i][0] - at) > radius:
            continue
        chunk = [_norm(w[2]).strip(".,!?") for w in words[i:i + len(target)]]
        if all(c.startswith(t) for c, t in zip(chunk, target)) and len(chunk) == len(target):
            d = abs(words[i][0] - at)
            if d < best_d:
                best, best_d = i, d
    return best


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--streamer", required=True)
    ap.add_argument("--at", required=True, help="HH:MM:SS de hoje (hora do PC)")
    ap.add_argument("--phrase", required=True)
    ap.add_argument("--score", type=float, required=True)
    ap.add_argument("--note", required=True)
    ap.add_argument("--day", default=time.strftime("%Y-%m-%d"))
    args = ap.parse_args()
    cfg = load_config()
    db = Database(cfg.settings.path("db_path"))
    at = datetime.strptime(f"{args.day} {args.at}", "%Y-%m-%d %H:%M:%S").timestamp()
    words = streamer_words(db, args.streamer, at - 6 * 3600)
    i = find_phrase(words, args.phrase, at)
    if i is None:
        print(f"frase '{args.phrase}' não encontrada perto de {args.at}"); return 2
    peak = words[i][0]
    base = peak - 60  # transcrição relativa a uma janela em volta do pico
    rel = [Word(w[0] - base, w[1] - base, w[2]) for w in words if base <= w[0] <= peak + 120]
    start_rel, end_rel = refine_bounds(60.0, Transcript(words=rel, text=" ".join(w.text for w in rel)), cfg.settings.get("detect.clip", {}))
    start, end = base + start_rel, base + end_rel
    inside = [w for w in words if start <= w[0] <= end]
    text = " ".join(w[2] for w in inside)
    seg_id, stream_id = words[i][4], words[i][3]
    reasons = {"notes": [f"curadoria: {args.note}"], "curated": True, "content": analyze(text), "chat": None,
               "audio": 0, "transcript": 0, "keywords": 0, "keywords_found": []}
    from clipador.detect.scorer import overlap_ratio

    dupes = [m for m in db.moments_overlapping(stream_id, start, end)
             if m["status"] not in ("rejected", "dupe", "failed") and overlap_ratio(start, end, m["start_ts"], m["end_ts"]) > 0.5]
    for m in dupes:  # o corte curado substitui o automático do mesmo trecho
        db.update("moments", m["id"], status="dupe")
        db.execute("UPDATE clips SET status='superseded', review_note='substituído por corte curado' WHERE moment_id=? AND status='ready'", [m["id"]])
    mid = db.add_moment(stream_id, seg_id, start, end, peak, args.score, reasons, text,
                        [[round(w[0], 3), round(w[1], 3), w[2]] for w in inside])
    print(f"momento {mid}: {time.strftime('%H:%M:%S', time.localtime(start))} +{end - start:.0f}s (nota {args.score:.0f})"
          f"{' substituiu ' + str([m['id'] for m in dupes]) if dupes else ''}")
    print("   " + text[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
