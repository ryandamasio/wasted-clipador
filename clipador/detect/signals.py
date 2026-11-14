"""Sinais por segundo (0..1) usados pela pontuação: chat, transcrição, palavras-chave, bloqueio."""
from __future__ import annotations

import math
import re
import unicodedata

import numpy as np

from clipador.detect.transcribe import Transcript

TRANSCRIPT_HYPE = [
    "caralho", "carai", "porra", "puta que pariu", "pqp", "meu deus", "não acredito", "nao acredito", "que isso",
    "que isso mano", "não não não", "nao nao nao", "olha isso", "olha isso aqui", "vai vai vai", "corre", "corre corre",
    "socorro", "mano", "véi", "velho", "cara", "não é possível", "nao e possivel", "impossível", "insano", "bizarro",
    "absurdo", "que loucura", "loucura", "ai ai ai", "nossa", "nossa senhora", "misericórdia", "brabo", "monstro",
    "lendário", "histórico", "pela primeira vez", "ganhei", "perdi", "morri", "morreu", "matei", "foi", "é isso",
]


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def chat_signal(messages: list[dict], seg_start_chat: float, total_seconds: int, baseline_rate: float,
                baseline_std: float, hype_tokens: list[str], min_burst_rate: float = 2.0) -> tuple[np.ndarray, dict]:
    """messages: já filtradas no intervalo do segmento (ts em tempo de chat).

    seg_start_chat = segment.start_ts - chat_offset.
    """
    rate = np.zeros(total_seconds, dtype=np.float32)
    hype = np.zeros(total_seconds, dtype=np.float32)
    toks = [_norm(t) for t in hype_tokens]
    for m in messages:
        sec = int(m["ts"] - seg_start_chat)
        if 0 <= sec < total_seconds:
            rate[sec] += 1
            t = _norm(m["text"])
            if any(tok in t for tok in toks) or re.search(r"(k{3,}|\?{3,}|!{3,}|(ha){2,}|rs{2,})", t):
                hype[sec] += 1
    # suaviza em janela de 5 s (reação do chat é espalhada)
    kernel = np.ones(5, dtype=np.float32) / 5.0
    rate_s = np.convolve(rate, kernel, mode="same")
    hype_s = np.convolve(hype, kernel, mode="same")
    base = max(baseline_rate, 0.05)
    std = max(baseline_std, base * 0.5, 0.1)
    signal = np.zeros(total_seconds, dtype=np.float32)
    for i in range(total_seconds):
        z = (rate_s[i] - base) / std
        hype_frac = hype_s[i] / rate_s[i] if rate_s[i] > 0 else 0.0
        # z alto = pico de velocidade; hype_frac alto = muita risada/espanto.
        # abs_factor evita que 3-4 mensagens num chat quase parado contem como explosão.
        abs_factor = min(1.0, rate_s[i] / max(min_burst_rate, 1e-6))
        signal[i] = abs_factor * (0.7 * _sigmoid((z - 1.5) * 1.5) + 0.3 * min(1.0, hype_frac * 2.0))
    info = {"messages": len(messages), "baseline_rate": round(base, 3), "peak_rate": round(float(rate_s.max()) if total_seconds else 0, 3)}
    return signal, info


def transcript_signal(transcript: Transcript, total_seconds: int) -> np.ndarray:
    """Interjeições + velocidade de fala acima da média."""
    signal = np.zeros(total_seconds, dtype=np.float32)
    if not transcript.words:
        return signal
    wps = np.zeros(total_seconds, dtype=np.float32)
    for w in transcript.words:
        sec = int(w.start)
        if 0 <= sec < total_seconds:
            wps[sec] += 1
    text_by_sec: dict[int, list[str]] = {}
    for w in transcript.words:
        sec = int(w.start)
        text_by_sec.setdefault(sec, []).append(_norm(w.text))
    mean_wps = float(wps[wps > 0].mean()) if (wps > 0).any() else 2.0
    hype_norm = [_norm(h) for h in TRANSCRIPT_HYPE]
    for sec in range(total_seconds):
        window_text = " ".join(" ".join(text_by_sec.get(s, [])) for s in range(max(0, sec - 2), min(total_seconds, sec + 3)))
        hits = sum(1 for h in hype_norm if h in window_text)
        speech = _sigmoid((wps[sec] - mean_wps) / max(mean_wps, 1.0) * 2.0) if wps[sec] > 0 else 0.0
        signal[sec] = min(1.0, 0.35 * speech + 0.35 * min(hits, 3) / 3.0 + (0.3 if hits >= 2 else 0.0))
    return signal


def keyword_signal(transcript: Transcript, keywords: list[str], total_seconds: int) -> tuple[np.ndarray, list[str]]:
    signal = np.zeros(total_seconds, dtype=np.float32)
    found: list[str] = []
    if not transcript.words:
        return signal, found
    kws = [(_norm(k), k) for k in keywords]
    # texto por segundo com contexto de 2 palavras (para keywords compostas)
    words = transcript.words
    for i, w in enumerate(words):
        chunk = _norm(" ".join(x.text for x in words[i:i + 3]))
        for nk, orig in kws:
            if chunk.startswith(nk + " ") or chunk == nk or (" " not in nk and _norm(w.text).startswith(nk)):
                sec = int(w.start)
                if 0 <= sec < total_seconds:
                    lo, hi = max(0, sec - 1), min(total_seconds, sec + 4)
                    signal[lo:hi] = np.maximum(signal[lo:hi], 1.0)
                if orig not in found:
                    found.append(orig)
    return signal, found


def blocklist_hits(transcript: Transcript, messages: list[dict], blocklist: list[str]) -> list[str]:
    hits: list[str] = []
    text = _norm(transcript.text) + " " + " ".join(_norm(m["text"]) for m in messages[:2000])
    for b in blocklist:
        if _norm(b) in text:
            hits.append(b)
    return hits
