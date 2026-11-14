"""Extração de áudio e energia (RMS) via ffmpeg + numpy."""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("detect.audio")
SAMPLE_RATE = 16000


def decode_audio(path: Path, start: float | None = None, duration: float | None = None,
                 concat_list: Path | None = None) -> np.ndarray:
    """Decodifica para float32 mono 16 kHz. Aceita arquivo único ou lista concat do ffmpeg."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if concat_list is not None:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_list)]
    else:
        cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou ao decodificar áudio: {proc.stderr.decode('utf-8', 'replace')[-400:]}")
    audio = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def rms_db_series(audio: np.ndarray, window_seconds: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Retorna (tempos_rel, rms_db) com um valor por janela."""
    win = max(1, int(SAMPLE_RATE * window_seconds))
    n = len(audio) // win
    if n == 0:
        return np.zeros(0), np.zeros(0)
    frames = audio[: n * win].reshape(n, win)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-10)
    db = 20 * np.log10(rms + 1e-9)
    times = np.arange(n) * window_seconds
    return times, db


def per_second_energy_signal(audio: np.ndarray, total_seconds: int, window_seconds: float = 0.5,
                             baseline_db: tuple[float, float] | None = None) -> tuple[np.ndarray, tuple[float, float]]:
    """Sinal 0..1 por segundo: quão acima do normal está o volume (gritaria, risada alta, explosão).

    baseline_db: (mediana, desvio) de segmentos anteriores para não normalizar só dentro do segmento.
    Retorna (sinal, (mediana, desvio)) para reaproveitar como baseline.
    """
    times, db = rms_db_series(audio, window_seconds)
    signal = np.zeros(total_seconds, dtype=np.float32)
    if len(db) == 0:
        return signal, (baseline_db or (-40.0, 6.0))
    voiced = db[db > -55]  # ignora silêncio absoluto
    med = float(np.median(voiced)) if len(voiced) else float(np.median(db))
    std = float(np.std(voiced)) if len(voiced) > 4 else 6.0
    std = max(std, 3.0)
    if baseline_db is not None:
        med = 0.5 * med + 0.5 * baseline_db[0]
        std = 0.5 * std + 0.5 * baseline_db[1]
    z = (db - med) / std
    # pico de energia = z alto; sigmoid centrada em z=1.2
    s = 1.0 / (1.0 + np.exp(-(z - 1.2) * 2.0))
    per_window_sec = (times).astype(int)
    for sec in range(total_seconds):
        mask = per_window_sec == sec
        if mask.any():
            signal[sec] = float(np.max(s[mask]))
    return signal, (med, std)
