"""Checagem de live online e descoberta de ferramentas (streamlink / yt-dlp / navegador)."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("capture.monitor")

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


@lru_cache(maxsize=1)
def find_browser(configured: str | None = "auto") -> str | None:
    """Navegador Chromium para o desafio JS da Kick (plugin do streamlink)."""
    if configured and configured != "auto":
        return configured if Path(configured).exists() else None
    for cand in BROWSER_CANDIDATES:
        if Path(cand).exists():
            return cand
    for name in ("chrome", "msedge", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def streamlink_cmd() -> list[str]:
    """Executa o streamlink do venv como módulo (evita depender do PATH)."""
    return [sys.executable, "-m", "streamlink"]


def ytdlp_cmd() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def _session(browser: str | None):
    from streamlink import Streamlink  # import tardio: carrega plugins

    session = Streamlink()
    session.set_option("stream-timeout", 60)
    session.set_option("hls-live-edge", 3)
    if browser:
        session.set_option("webbrowser-executable", browser)
        session.set_option("webbrowser-headless", True)
    return session


def check_online(url: str, platform: str, browser: str | None, timeout: int = 45) -> tuple[bool, str | None]:
    """Retorna (online, título). Usa streamlink; cai para yt-dlp se o plugin falhar."""
    try:
        session = _session(browser)
        plugin_name, plugin_cls, resolved = session.resolve_url(url)
        plugin = plugin_cls(session, resolved)
        streams = plugin.streams()
        if streams:
            title = None
            try:
                title = plugin.get_title()
            except Exception:  # noqa: BLE001 - metadado opcional
                pass
            return True, title
        return False, None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "No playable streams" in msg or "offline" in msg.lower():
            return False, None
        log.warning("streamlink falhou ao checar %s (%s); tentando yt-dlp", url, msg[:160])
    return _check_online_ytdlp(url, timeout)


def _check_online_ytdlp(url: str, timeout: int) -> tuple[bool, str | None]:
    try:
        proc = subprocess.run(
            [*ytdlp_cmd(), "--quiet", "--no-warnings", "--simulate", "--print", "%(is_live)s|%(title)s", url],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timeout ao checar %s", url)
        return False, None
    if proc.returncode != 0:
        return False, None
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return False, None
    is_live, _, title = line[-1].partition("|")
    return is_live.strip().lower() == "true", (title.strip() or None)
