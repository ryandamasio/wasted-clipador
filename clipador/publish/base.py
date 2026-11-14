"""Contrato dos publishers + utilitários (tokens persistidos em data/tokens, cliente HTTP)."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("publish")


@dataclass
class PostPayload:
    video: Path
    title: str
    caption: str                      # descrição/legenda completa (já com hashtags e crédito)
    tags: list[str] = field(default_factory=list)   # sem '#'
    cover: Path | None = None
    clip_id: int | None = None
    post_id: int | None = None


@dataclass
class PublishResult:
    url: str | None = None
    remote_id: str | None = None
    status: str = "posted"            # posted | processing (plataforma ainda processando, mas aceito)
    raw: dict = field(default_factory=dict)


class PublishError(Exception):
    """retryable=False para erros definitivos (credencial inválida, vídeo rejeitado)."""

    def __init__(self, message: str, retryable: bool = True, code: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class Publisher:
    name = "base"
    manual = False                     # True: ninguém chama publish(); o dono posta à mão e marca na fila

    def __init__(self, cfg_net: dict, cfg_all: dict, tokens_dir: Path):
        self.cfg = cfg_net or {}
        self.all = cfg_all or {}
        self.tokens = TokenStore(tokens_dir / f"{self.name}.json")

    def configured(self) -> tuple[bool, str]:
        """(ok, motivo). Não faz rede."""
        return True, "ok"

    def publish(self, payload: PostPayload) -> PublishResult:  # pragma: no cover - abstrato
        raise NotImplementedError

    def fetch_metrics(self, remote_id: str) -> dict | None:
        return None

    # helpers
    def http(self, timeout: float = 120.0) -> httpx.Client:
        return httpx.Client(timeout=timeout, follow_redirects=True, transport=_TRANSPORT)


class TokenStore:
    """Tokens que mudam (TikTok rotaciona refresh token; access tokens expiram) ficam em data/tokens/<rede>.json.

    O arquivo é local (data/ está no .gitignore). Os segredos fixos (client id/secret) continuam só no .env.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict | None = None

    def load(self) -> dict:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            except Exception:  # noqa: BLE001
                self._data = {}
        return self._data

    def get(self, key: str, env: str | None = None, default=None):
        val = self.load().get(key)
        if val in (None, "") and env:
            val = os.environ.get(env) or None
        return val if val not in (None, "") else default

    def save(self, **values) -> None:
        data = self.load()
        data.update(values)
        data["updated_at"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._data = data


# transporte injetável nos testes (httpx.MockTransport)
_TRANSPORT: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _TRANSPORT
    _TRANSPORT = transport


def env(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    return v or None


def error_text(resp: httpx.Response, limit: int = 300) -> str:
    try:
        j = resp.json()
        if isinstance(j, dict):
            err = j.get("error") or j
            if isinstance(err, dict):
                return str(err.get("message") or err.get("error_description") or err)[:limit]
            return str(err)[:limit]
    except Exception:  # noqa: BLE001
        pass
    return resp.text[:limit]
