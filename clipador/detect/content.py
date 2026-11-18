"""O que está sendo dito no trecho: conversa com o chat (subs, doações) x momento com cara de viral no GTA 6.

O pico de chat engana: o chat explode quando o streamer agradece inscrições ou lê mensagens. A fala desmascara.
"""
from __future__ import annotations

import re

from clipador.detect.signals import _norm

# falando com o chat / agradecendo apoio (texto já normalizado: minúsculo, sem acento)
CHAT_TALK = [
    r"\bsubs?\b", r"\binscri\w*", r"\bmeses\b", r"\bprime\b", r"\btier\b", r"\bbits\b", r"\bdoac\w*", r"\bdoou\b", r"\bpix\b",
    r"\bvaleu\w* pel[oa]s?\b", r"\bobrigad\w* pel[oa]s?\b", r"\bmuito obrigad\w*", r"\btamo junto\b", r"\bum beijo\b",
    r"\bseja bem.?vind\w*", r"\bfollow\b", r"\bseguir o canal\b", r"\bsorteio\b", r"\bcupom\b", r"\bmandou\b .{0,20}\b(reais|conto)\b",
    r"\bchat\b", r"\bpessoal do (youtube|chat)\b",
]
# reação/primeira impressão que prende quem assiste
VIRAL = [
    r"que grafico", r"\bolha (isso|isso aqui|o mar|a agua|o tamanho|a praia|o ceu|esse|essa|que|o cara|ali)\b", r"\bo mar\b",
    r"\bpraia\b", r"que lindo", r"\brealista\b", r"meu deus", r"nao acredito", r"\babsurd\w*", r"\bperfeit\w*", r"vice city",
    r"\blucia\b", r"\bjason\b", r"\bpolicia\b", r"\bjacare\w*", r"\binsan\w*", r"\bbizarr\w*", r"\bque isso\b", r"\bbugou\b",
    r"\bmorr(i|eu)\b", r"\bexplodiu\b", r"\bfodeu\b", r"\bcorre\b", r"\bsocorro\b", r"nao e possivel", r"\bimpossivel\b",
    r"\bprimeira vez\b", r"\bfinalmente\b", r"\bchorando\b", r"\barrepi\w*", r"\bmano do ceu\b", r"\bpqp\b", r"\bputa que pariu\b",
]
_CHAT_RE = [re.compile(p) for p in CHAT_TALK]
_VIRAL_RE = [re.compile(p) for p in VIRAL]


def analyze(text: str) -> dict:
    """Quantos padrões distintos de cada tipo aparecem no trecho, e quais (para o motivo do corte)."""
    t = _norm(text or "")
    chat = [p.pattern for p in _CHAT_RE if p.search(t)]
    viral = [m.group(0) for p in _VIRAL_RE for m in [p.search(t)] if m]
    return {"chat_talk": len(chat), "viral": viral}


def adjust(score: float, info: dict, talk_penalty: float = 0.5, viral_bonus: float = 5.0, viral_max: float = 20.0) -> float:
    """Conversa com o chat dominando o trecho derruba a nota; reações de GTA 6 sobem a nota."""
    talk, viral = info["chat_talk"], len(info["viral"])
    if talk >= 2 and talk > viral:
        return score * talk_penalty
    return min(100.0, score + min(viral_max, viral_bonus * viral))
