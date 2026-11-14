"""Transcrição local com faster-whisper (CUDA, com fallback para CPU).

Um único modelo carregado por processo, protegido por lock (VRAM de 6 GB).
"""
from __future__ import annotations

import logging
import os
import site
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("detect.transcribe")


def _add_nvidia_dlls() -> None:
    """As DLLs cuBLAS/cuDNN vêm dos pacotes pip nvidia-*; precisam estar no PATH no Windows."""
    if os.name != "nt":
        return
    roots = set(site.getsitepackages() + [site.getusersitepackages()])
    for root in roots:
        nv = Path(root) / "nvidia"
        if not nv.exists():
            continue
        for sub in nv.iterdir():
            bin_dir = sub / "bin"
            if bin_dir.is_dir():
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                try:
                    os.add_dll_directory(str(bin_dir))
                except (AttributeError, OSError):
                    pass


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float = 1.0


@dataclass
class Transcript:
    words: list[Word]
    text: str
    language: str = "pt"

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.end > start and w.start < end]

    def to_json(self) -> dict:
        return {"text": self.text, "language": self.language,
                "words": [[round(w.start, 3), round(w.end, 3), w.text, round(w.prob, 3)] for w in self.words]}

    @classmethod
    def from_json(cls, data: dict) -> "Transcript":
        return cls(words=[Word(w[0], w[1], w[2], w[3] if len(w) > 3 else 1.0) for w in data.get("words", [])],
                   text=data.get("text", ""), language=data.get("language", "pt"))


class Transcriber:
    _instance: "Transcriber | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.model = None
        self.device = cfg.get("device", "cuda")
        self.compute_type = cfg.get("compute_type", "int8_float16")

    @classmethod
    def get(cls, cfg: dict) -> "Transcriber":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(cfg)
            return cls._instance

    def _load(self) -> None:
        if self.model is not None:
            return
        from faster_whisper import WhisperModel

        _add_nvidia_dlls()
        name = self.cfg.get("model", "small")
        download_root = os.environ.get("HF_HOME")
        for device, compute in ((self.device, self.compute_type), ("cpu", "int8")):
            try:
                log.info("carregando Whisper %s em %s (%s)...", name, device, compute)
                self.model = WhisperModel(name, device=device, compute_type=compute, download_root=download_root,
                                          cpu_threads=4, num_workers=1)
                # sanity check rápido para capturar erro de DLL/VRAM já no carregamento
                list(self.model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, language="pt")[0])
                self.device, self.compute_type = device, compute
                log.info("Whisper pronto (%s/%s)", device, compute)
                return
            except Exception as exc:  # noqa: BLE001
                log.error("falha ao iniciar Whisper em %s: %s", device, str(exc)[:300])
                self.model = None
                if device == "cpu":
                    raise

    def transcribe(self, audio: np.ndarray, initial_prompt: str | None = None) -> Transcript:
        with self.lock:
            self._load()
            if len(audio) < 1600:
                return Transcript(words=[], text="")
            prompt = initial_prompt or "Live de GTA 6 no Brasil. Streamer jogando e reagindo, com gírias: mano, véi, kkkk, pqp."
            segments, info = self.model.transcribe(
                audio,
                language=self.cfg.get("language", "pt"),
                beam_size=int(self.cfg.get("beam_size", 2)),
                word_timestamps=True,
                vad_filter=bool(self.cfg.get("vad_filter", True)),
                vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 150},
                condition_on_previous_text=False,
                initial_prompt=prompt,
                no_speech_threshold=0.6,
            )
            words: list[Word] = []
            texts: list[str] = []
            for seg in segments:
                texts.append(seg.text.strip())
                for w in seg.words or []:
                    t = w.word.strip()
                    if t:
                        words.append(Word(float(w.start), float(w.end), t, float(w.probability or 1.0)))
            return Transcript(words=words, text=" ".join(t for t in texts if t), language=info.language or "pt")
