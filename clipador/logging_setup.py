"""Logging para console + arquivo rotativo."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(logs_dir: Path, level: int = logging.INFO, name: str = "clipador.log") -> None:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    fh = RotatingFileHandler(logs_dir / name, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)
    # Bibliotecas barulhentas
    for noisy in ("httpx", "httpx2", "httpcore", "websockets", "streamlink", "urllib3", "faster_whisper",
                  "chat_downloader", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
