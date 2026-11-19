"""Túnel público para a fila de aprovação via Cloudflare (quick tunnel, sem conta): https://xxxx.trycloudflare.com

Exige senha (REVIEW_PASSWORD no .env), senão qualquer pessoa com o link aprovaria clipes. A URL muda a cada
reinício; fica em data/tunnel_url.txt, no `clipador status` e no Painel.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("review.tunnel")
CANDIDATES = [r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"]
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for c in CANDIDATES:
        if Path(c).exists():
            return c
    return None


class Tunnel(threading.Thread):
    def __init__(self, port: int, url_file: Path):
        super().__init__(name="tunnel", daemon=True)
        self.port = port
        self.url_file = url_file
        self.url: str | None = None
        self.stop_event = threading.Event()
        self.proc: subprocess.Popen | None = None

    def run(self) -> None:
        self.url_file.unlink(missing_ok=True)  # endereço de uma execução anterior morta não vale mais
        exe = find_cloudflared()
        if not exe:
            log.error("cloudflared não encontrado: `winget install Cloudflare.cloudflared`")
            return
        threading.Thread(target=self._watchdog, name="tunnel-watchdog", daemon=True).start()
        while not self.stop_event.is_set():
            creation = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}"],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=creation)
            try:
                for raw in iter(self.proc.stderr.readline, b""):
                    line = raw.decode("utf-8", "replace")
                    m = URL_RE.search(line)
                    if m and m.group(0) != self.url:
                        self.url = m.group(0)
                        self.url_file.parent.mkdir(parents=True, exist_ok=True)
                        self.url_file.write_text(self.url, encoding="utf-8")
                        log.info("fila de aprovação na internet: %s (senha do .env)", self.url)
                    if self.stop_event.is_set():
                        break
            finally:
                self._kill()
            if self.stop_event.is_set():
                break
            log.warning("túnel caiu; reabrindo em 20s")
            self.url = None
            self.url_file.unlink(missing_ok=True)
            self.stop_event.wait(20)

    def _watchdog(self, every: float = 300, max_fails: int = 3) -> None:
        # quick tunnel às vezes morre do lado da Cloudflare com o cloudflared ainda vivo (18/11: link deu 000 por horas);
        # derrubar o processo faz o laço do run() abrir um túnel novo
        fails = 0
        while not self.stop_event.wait(every):
            url = self.url
            if not url:
                fails = 0
                continue
            try:
                ok = httpx.get(url, timeout=20).status_code < 500   # 401 (pede senha) = vivo
            except httpx.HTTPError:
                ok = False
            fails = 0 if ok else fails + 1
            if fails >= max_fails:
                log.warning("túnel %s não responde há %d checagens; reabrindo", url, fails)
                fails = 0
                self._kill()

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        self._kill()
        self.url_file.unlink(missing_ok=True)


def current_url(url_file: Path) -> str | None:
    try:
        return url_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
