"""Pontuação opcional por LLM barato (Anthropic Haiku). Desligado por padrão.

Só é chamado para janelas que já passaram de `min_score_to_call` nas heurísticas, para economizar.
Retorna None quando desabilitado ou sem chave: o pipeline segue 100% local.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("detect.llm")

PROMPT = """Você avalia trechos de transcrição de lives brasileiras de GTA 6 para virar cortes curtos (15-40 s).
Dê uma nota de 0 a 10 para o potencial de clipe viral considerando: engraçado, chocante, épico, reação forte,
momento relevante de GTA 6. Penalize conteúdo ofensivo, discurso de ódio, conteúdo sexual ou que exponha o streamer
de forma problemática (nota 0). Responda SOMENTE em JSON: {{"score": <0-10>, "reason": "<até 12 palavras>", "title": "<título curto com gancho, pt-BR, sem clickbait enganoso>"}}

Streamer: {streamer}
Contexto do chat (amostra): {chat}
Transcrição: {text}"""


class LLMScorer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled")) and bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.model = cfg.get("model", "claude-haiku-5-5")
        self._client = None
        if cfg.get("enabled") and not self.enabled:
            log.warning("detect.llm.enabled=true mas ANTHROPIC_API_KEY ausente no .env; seguindo sem LLM")

    def score(self, streamer: str, text: str, chat_sample: list[str]) -> dict | None:
        if not self.enabled or not text.strip():
            return None
        try:
            if self._client is None:
                import anthropic

                self._client = anthropic.Anthropic()
            prompt = PROMPT.format(streamer=streamer, chat=" | ".join(chat_sample[:12])[:600], text=text[:1500])
            resp = self._client.messages.create(model=self.model, max_tokens=200,
                                                messages=[{"role": "user", "content": prompt}])
            raw = "".join(getattr(b, "text", "") for b in resp.content)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else {}
            score = float(data.get("score", 0))
            return {"score": max(0.0, min(10.0, score)), "reason": str(data.get("reason", ""))[:120],
                    "title": str(data.get("title", ""))[:90]}
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM falhou (%s); seguindo sem ele", str(exc)[:200])
            return None
