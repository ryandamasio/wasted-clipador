"""Carregamento de configuração (settings.yaml, streamers.yaml, hashtags.yaml, .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def _load_env(path: Path) -> None:
    """Carrega .env simples (KEY=VALUE) sem dependência externa."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Streamer:
    name: str
    handle: str
    platform: str
    url: str
    priority: int = 5
    display: str = ""
    channel_link: str = ""
    enabled: bool = True
    facecam: dict | None = None
    chat_offset_seconds: float | None = None

    def __post_init__(self) -> None:
        self.platform = self.platform.lower()
        if self.platform not in ("twitch", "kick", "youtube"):
            raise ValueError(f"Plataforma inválida para {self.name}: {self.platform}")
        self.display = self.display or f"@{self.handle}"
        self.channel_link = self.channel_link or self.url


class Settings:
    """Acesso com caminho pontilhado: settings.get('detect.weights.chat')."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return dict(self._data.get(name, {}))

    def path(self, key: str) -> Path:
        """Resolve um caminho de settings.paths em relação à raiz do projeto."""
        raw = self.get(f"paths.{key}")
        if raw is None:
            raise KeyError(f"paths.{key} não definido em settings.yaml")
        return self.resolve(raw)

    @staticmethod
    def resolve(raw: str | Path) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class Config:
    settings: Settings
    streamers: list[Streamer]
    hashtags: dict[str, Any]
    root: Path = PROJECT_ROOT

    def streamer(self, handle: str) -> Streamer | None:
        for s in self.streamers:
            if s.handle == handle:
                return s
        return None

    @property
    def enabled_streamers(self) -> list[Streamer]:
        blocked = self.blocklist()
        return sorted((s for s in self.streamers if s.enabled and s.handle not in blocked), key=lambda s: s.priority)

    # --- lista de bloqueio (pedido de remoção de streamer) ---------------------
    @staticmethod
    def blocklist_path() -> Path:
        return CONFIG_DIR / "blocklist.yaml"

    def blocklist(self) -> set[str]:
        p = self.blocklist_path()
        if not p.exists():
            return set()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return {str(h).lower() for h in (data.get("streamers") or [])}

    def block_streamer(self, handle: str, reason: str = "") -> None:
        p = self.blocklist_path()
        data = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {}
        items = list(data.get("streamers") or [])
        if handle.lower() not in {str(h).lower() for h in items}:
            items.append(handle.lower())
        data["streamers"] = items
        notes = dict(data.get("notes") or {})
        if reason:
            notes[handle.lower()] = reason
        data["notes"] = notes
        p.write_text("# Streamers bloqueados (pedido de remoção). Nunca serão gravados nem clipados, mesmo com enabled: true.\n"
                     + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def ensure_dirs(self) -> None:
        for key in ("data_dir", "recordings_dir", "clips_dir", "output_dir", "models_dir", "logs_dir"):
            self.settings.path(key).mkdir(parents=True, exist_ok=True)
        # Cache de modelos Whisper fora do C:
        os.environ.setdefault("HF_HOME", str(self.settings.path("models_dir")))


_CONFIG: Config | None = None


def load_config(reload: bool = False) -> Config:
    global _CONFIG
    if _CONFIG is not None and not reload:
        return _CONFIG
    _load_env(PROJECT_ROOT / ".env")
    settings = Settings(yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8")) or {})
    raw_streamers = yaml.safe_load((CONFIG_DIR / "streamers.yaml").read_text(encoding="utf-8")) or {}
    streamers = [Streamer(**s) for s in raw_streamers.get("streamers", [])]
    hashtags = yaml.safe_load((CONFIG_DIR / "hashtags.yaml").read_text(encoding="utf-8")) or {}
    _CONFIG = Config(settings=settings, streamers=streamers, hashtags=hashtags)
    _CONFIG.ensure_dirs()
    return _CONFIG
