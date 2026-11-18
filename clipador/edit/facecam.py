"""Acha a câmera do streamer no próprio trecho do corte (os streamers mudam a câmera de canto entre cenas).

Rosto do streamer = rosto que fica parado no mesmo lugar, numa faixa lateral da tela, na maioria dos quadros.
Rostos do jogo (cutscene) se movem e costumam estar no centro: ficam de fora.
"""
from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("edit.facecam")
W, H = 960, 540


def _frames(concat: Path, offset: float, duration: float, n: int = 6) -> list[np.ndarray]:
    out = []
    for k in range(1, n + 1):
        t = offset + duration * k / (n + 1)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{t:.2f}", "-f", "concat", "-safe", "0",
                            "-i", str(concat), "-frames:v", "1", "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                           capture_output=True, timeout=60)
        if len(r.stdout) == W * H * 3:
            out.append(np.frombuffer(r.stdout, dtype=np.uint8).reshape(H, W, 3))
    return out


def faces(frame: np.ndarray) -> list[tuple[float, float, float, float]]:
    """(cx, cy, w, h) em frações da tela."""
    import cv2

    gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    out: list[tuple[float, float, float, float]] = []
    # dois detectores: o padrão erra rosto com óculos/luz baixa que o alt2 pega
    for xml, nb in (("haarcascade_frontalface_default.xml", 5), ("haarcascade_frontalface_alt2.xml", 4)):
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + xml)
        for x, y, w, h in cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=nb, minSize=(22, 22)):
            f = ((x + w / 2) / W, (y + h / 2) / H, w / W, h / H)
            if all(math.dist(f[:2], o[:2]) > 0.03 for o in out):
                out.append(f)
    return out


def choose(per_frame: list[list[tuple[float, float, float, float]]], min_share: float = 0.6) -> dict | None:
    """Agrupa rostos parados numa faixa lateral e devolve a caixa da câmera {x, y, w, h} (frações) ou None."""
    n = len(per_frame)
    if n == 0:
        return None
    side = [[f for f in fr if (f[0] < 0.34 or f[0] > 0.66) and f[3] < 0.3] for fr in per_frame]
    best, best_hits = None, 0
    for fr in side:
        for c in fr:
            group = [min(o, key=lambda f: math.dist(f[:2], c[:2])) for o in side if o and min(math.dist(f[:2], c[:2]) for f in o) < 0.04]
            if len(group) > best_hits:
                best, best_hits = group, len(group)
    if not best or best_hits < math.ceil(min_share * n):
        return None
    cx, cy, fw, fh = (float(np.median([g[i] for g in best])) for i in range(4))
    bw, bh = min(0.30, max(0.12, fw * 3.5)), min(0.34, max(0.15, fh * 2.3))  # proporções medidas nas câmeras reais
    x, y = cx - bw / 2, cy - bh / 2
    x = 0.0 if x < 0.03 else (1 - bw if x + bw > 0.97 else x)   # câmeras ficam encostadas na borda
    y = 0.0 if y < 0.03 else (1 - bh if y + bh > 0.97 else y)
    return {"x": round(max(0.0, x), 3), "y": round(max(0.0, y), 3), "w": round(bw, 3), "h": round(bh, 3)}


def detect(concat: Path, offset: float, duration: float) -> dict | None:
    try:
        frames = _frames(concat, offset, duration)
        box = choose([faces(f) for f in frames])
    except Exception as exc:  # noqa: BLE001
        log.warning("detecção da câmera falhou: %s", exc)
        return None
    log.info("câmera do streamer no trecho: %s", box or "não encontrada (layout centralizado)")
    return box
