"""Curadoria: linha do tempo da fala de uma live com os trechos candidatos a corte, para leitura humana.

Fonte 1 (gravação do serviço): --stream ID [--stream ID2 ...] usa as transcrições em cache dos segmentos e o chat.
Fonte 2 (VOD/áudio solto):   --audio arquivo.m4a [--offset-label HH:MM] transcreve com o Whisper e salva .words.json.

Imprime janelas de 45 s ordenadas por "potencial" (reações virais na fala, gritos, chat acima da média,
descontando conversa com o chat/subs), com o texto da janela, para escolher os cortes à mão.

Uso:
  .venv/Scripts/python scripts/scan.py --stream 55 --top 15
  .venv/Scripts/python scripts/scan.py --audio data/backfill/alanzoka.m4a --top 25
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipador.config import load_config  # noqa: E402
from clipador.detect.content import analyze  # noqa: E402
from clipador.detect.transcribe import Transcript  # noqa: E402
from clipador.store.db import Database  # noqa: E402

WIN, STEP = 45, 15


def words_from_stream(db: Database, stream_id: int) -> tuple[list[tuple[float, float, str]], float]:
    segs = db.fetchall("SELECT * FROM segments WHERE stream_id=? AND status!='deleted' ORDER BY idx", [stream_id])
    words = []
    for s in segs:
        tp = Path(s["path"]).with_suffix(".transcript.json")
        if not tp.exists():
            continue
        t = Transcript.from_json(json.loads(tp.read_text(encoding="utf-8")))
        words += [(s["start_ts"] + w.start, s["start_ts"] + w.end, w.text) for w in t.words]
    return words, (segs[0]["start_ts"] if segs else 0.0)


def words_from_audio(path: Path) -> list[tuple[float, float, str]]:
    cache = path.with_suffix(".words.json")
    if cache.exists():
        return [tuple(w) for w in json.loads(cache.read_text(encoding="utf-8"))]
    from clipador.detect.audio import decode_audio
    from clipador.detect.transcribe import Transcriber

    cfg = load_config()
    tr = Transcriber.get(cfg.settings.get("detect.whisper", {}))
    words: list = []
    chunk = 600  # 10 min por vez (memória)
    pos = 0.0
    while True:
        audio = decode_audio(path, start=pos, duration=chunk)
        if len(audio) < 16000:
            break
        t = tr.transcribe(audio)
        words += [(round(pos + w.start, 2), round(pos + w.end, 2), w.text) for w in t.words]
        print(f"  transcrito até {int((pos + chunk) // 60)} min ({len(words)} palavras)", file=sys.stderr)
        pos += chunk
    cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


def english_marks(path: Path, chunk: int = 30) -> dict[int, float]:
    """{início do bloco de 30 s: prob. de inglês} (diálogo do jogo = cutscene). Cache em .lang.json."""
    cache = path.with_suffix(".lang.json")
    if cache.exists():
        return {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    import numpy as _np

    from clipador.detect.audio import decode_audio
    from clipador.detect.transcribe import Transcriber

    tr = Transcriber.get(load_config().settings.get("detect.whisper", {}))
    tr.transcribe(_np.zeros(16000, dtype=_np.float32))
    marks, pos = {}, 0
    while True:
        a = decode_audio(path, start=pos, duration=chunk)
        if len(a) < 16000 * 8:
            break
        with tr.lock:
            _, _, probs = tr.model.detect_language(a)
        marks[pos] = round(dict(probs).get("en", 0.0), 2)
        pos += chunk
    cache.write_text(json.dumps(marks), encoding="utf-8")
    return marks


def scan(words, origin: float, chat_ts: list[float], top: int, label_fmt, en_marks: dict[int, float] | None = None) -> None:
    if not words:
        print("sem transcrição"); return
    t_end = words[-1][1]
    chat = np.array(sorted(chat_ts)) if chat_ts else np.array([])
    rate_all = len(chat) / max(t_end - origin, 1) if len(chat) else 0
    rows = []
    t = origin
    wi = 0
    while t < t_end:
        while wi < len(words) and words[wi][0] < t:
            wi += 1
        j = wi
        txt = []
        while j < len(words) and words[j][0] < t + WIN:
            txt.append(words[j][2]); j += 1
        text = " ".join(txt)
        info = analyze(text)
        n_chat = int(((chat >= t) & (chat < t + WIN)).sum()) if len(chat) else 0
        chat_ratio = (n_chat / WIN) / rate_all if rate_all else 0
        wps = len(txt) / WIN
        score = 3 * len(info["viral"]) + min(chat_ratio, 4) + (1 if wps > 3 else 0) - 2.5 * max(0, info["chat_talk"] - len(info["viral"]))
        en = max((v for k, v in (en_marks or {}).items() if k < t + WIN and k + 30 > t), default=0.0)
        if en >= 0.5:
            score += 4  # cutscene: cena cinematográfica do jogo
            info = {**info, "viral": ["CENA DO JOGO (inglês %.0f%%)" % (en * 100)] + info["viral"]}
        rows.append((score, t, info, chat_ratio, text))
        t += STEP
    rows.sort(key=lambda r: -r[0])
    picked: list[float] = []
    for score, t, info, cr, text in rows:
        if any(abs(t - p) < WIN for p in picked):
            continue
        picked.append(t)
        print(f"\n[{label_fmt(t)}] potencial {score:.1f} | chat x{cr:.1f} | reações {info['viral']} | conversa-chat {info['chat_talk']}")
        print("   " + text[:700])
        if len(picked) >= top:
            break


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", type=int, action="append")
    ap.add_argument("--streamer", help="todas as gravações desse streamer desde --since")
    ap.add_argument("--since", default="00:00", help="HH:MM de hoje")
    ap.add_argument("--audio")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    cfg = load_config()
    db = Database(cfg.settings.path("db_path"))
    import time as _t
    if args.streamer:
        since = _t.mktime(_t.strptime(_t.strftime("%Y-%m-%d ") + args.since, "%Y-%m-%d %H:%M"))
        args.stream = [r["id"] for r in db.fetchall(
            "SELECT DISTINCT s.id FROM streams s JOIN segments g ON g.stream_id=s.id WHERE s.streamer=? AND g.start_ts>=? AND s.url NOT LIKE '%/videos/%'",
            [args.streamer, since])]
    if args.audio:
        words = words_from_audio(Path(args.audio))
        scan(words, 0.0, [], args.top, lambda s: f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(s % 60):02d} do arquivo",
             english_marks(Path(args.audio)))
    if args.stream:
        # várias streams (o serviço abre uma nova a cada reinício) viram uma linha do tempo só
        words, chat, origins = [], [], []
        off = float(cfg.settings.get("capture.chat_offset_seconds", 7))
        for sid in args.stream:
            w, o = words_from_stream(db, sid)
            words += w
            origins.append(o)
            chat += [m["ts"] + off for m in db.chat_between(sid, 0, 9e12)]
        words.sort()
        st = db.get_stream(args.stream[0])
        print(f"\n===== {st['streamer']} ({st['title']}) streams {args.stream} =====")
        scan(words, min(o for o in origins if o) if any(origins) else 0.0, chat, args.top,
             lambda s: _t.strftime("%H:%M:%S", _t.localtime(s)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
