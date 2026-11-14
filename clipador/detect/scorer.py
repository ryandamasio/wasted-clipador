"""Combina sinais por segundo em nota 0-100 por janela e escolhe os momentos."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from clipador.detect.transcribe import Transcript


@dataclass
class Candidate:
    peak_rel: float          # segundo (relativo ao segmento) do pico
    window_start: float
    window_end: float
    score: float
    components: dict = field(default_factory=dict)
    start_rel: float = 0.0   # limites finais do clipe (relativos ao segmento)
    end_rel: float = 0.0


def combine(signals: dict[str, np.ndarray | None], weights: dict[str, float], window_seconds: int) -> tuple[np.ndarray, dict]:
    """Soma ponderada dos sinais disponíveis, com pesos renormalizados quando um sinal falta.

    Retorna (nota 0-100 por segundo de início de janela, pesos efetivos).
    """
    available = {k: v for k, v in signals.items() if v is not None and len(v) > 0}
    if not available:
        return np.zeros(0), {}
    total_w = sum(weights.get(k, 0.0) for k in available)
    eff = {k: weights.get(k, 0.0) / total_w for k in available} if total_w > 0 else {k: 1 / len(available) for k in available}
    n = min(len(v) for v in available.values())
    kernel = np.ones(window_seconds, dtype=np.float32) / window_seconds
    combined = np.zeros(n, dtype=np.float32)
    for k, v in available.items():
        v = np.asarray(v[:n], dtype=np.float32)
        windowed = np.convolve(v, kernel, mode="same")
        # usa max entre média da janela e 0.6*pico para não diluir picos curtos
        peak = np.array([v[max(0, i - window_seconds // 2): i + window_seconds // 2 + 1].max() for i in range(n)])
        combined += eff[k] * np.maximum(windowed, 0.6 * peak)
    return np.clip(combined * 100.0, 0, 100), eff


def pick_peaks(score: np.ndarray, min_score: float, min_gap: int, max_peaks: int) -> list[int]:
    order = np.argsort(score)[::-1]
    chosen: list[int] = []
    for idx in order:
        if score[idx] < min_score or len(chosen) >= max_peaks:
            break
        if all(abs(int(idx) - c) >= min_gap for c in chosen):
            chosen.append(int(idx))
    return sorted(chosen)


def refine_bounds(peak_rel: float, transcript: Transcript | None, clip_cfg: dict, media_end_rel: float | None = None) -> tuple[float, float]:
    """Define início/fim do clipe: gancho logo no pico, sem cortar frase no meio.

    - start = pico - pre_roll, ajustado para o início de uma frase/palavra próxima.
    - end   = pausa de fala mais próxima do alvo dentro de [min, max].
    """
    pre_roll = float(clip_cfg.get("pre_roll_seconds", 1.5))
    min_s = float(clip_cfg.get("min_seconds", 15))
    max_s = float(clip_cfg.get("max_seconds", 40))
    target = float(clip_cfg.get("target_seconds", 25))
    start = max(0.0, peak_rel - pre_roll)
    words = sorted(transcript.words, key=lambda w: w.start) if transcript and transcript.words else []

    if words:
        # início de frase: palavra precedida de pausa >= 0.5 s, entre (start - 3 s) e o pico.
        # Escolhe a mais próxima de `start`; empate favorece começar antes (não perder o gancho).
        best, best_dist = None, 1e9
        for i, w in enumerate(words):
            if w.start > peak_rel + 0.3:
                break
            if w.start < start - 3.0:
                continue
            gap = w.start - words[i - 1].end if i > 0 else 9.0
            if gap >= 0.5:
                dist = abs(w.start - start) + (0.3 if w.start > start else 0.0)
                if dist < best_dist:
                    best, best_dist = w.start, dist
        if best is None:
            # senão, não cortar palavra: recua para o início da palavra que contém `start`,
            # ou avança para a próxima palavra se `start` cair numa pausa
            for w in words:
                if w.start <= start < w.end:
                    best = w.start
                    break
                if w.start > start:
                    best = w.start if w.start - start < 1.0 else None
                    break
        if best is not None:
            start = max(0.0, best - 0.25)

    end = start + target
    if words:
        lo, hi = start + min_s, start + max_s
        best_end, best_dist = None, 1e9
        for i in range(len(words) - 1):
            pause_at = words[i].end
            gap = words[i + 1].start - pause_at
            if lo <= pause_at <= hi and gap >= 0.45:
                dist = abs(pause_at - (start + target))
                # prefere pausas maiores (fim de frase) quando a distância é parecida
                dist -= min(gap, 2.0) * 1.5
                if dist < best_dist:
                    best_dist, best_end = dist, pause_at
        if best_end is None:
            # fim da última palavra dentro da faixa
            inside = [w.end for w in words if lo <= w.end <= hi]
            if inside:
                best_end = min(inside, key=lambda e: abs(e - (start + target)))
        if best_end is not None:
            end = best_end + 0.35
    if media_end_rel is not None:
        end = min(end, media_end_rel)
    end = max(end, start + min_s)
    end = min(end, start + max_s)
    return round(start, 2), round(end, 2)


def overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    shortest = max(1e-6, min(a1 - a0, b1 - b0))
    return inter / shortest
