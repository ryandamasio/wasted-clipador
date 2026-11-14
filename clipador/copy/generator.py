"""Títulos, descrições e hashtags por rede (templates locais; LLM opcional complementa)."""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

from clipador.config import Config, Streamer

NETWORKS = ("youtube", "tiktok", "instagram", "kwai")

HOOKS_GENERIC = [
    "{name} não acreditou no que aconteceu",
    "Olha o que aconteceu com {name} no GTA 6",
    "{name} surtou com isso no GTA 6",
    "O momento que o chat do {name} explodiu",
    "{name} passou por isso no GTA 6 😳",
    "Ninguém esperava isso na live do {name}",
]
HOOKS_BY_REASON = {
    "chat": ["O chat do {name} foi à loucura com isso", "O chat explodiu nesse momento do {name}"],
    "audio": ["{name} gritou nessa hora 😂", "A reação do {name} nesse momento"],
    "keywords": ["{name} e a {kw} mais insana do GTA 6", "{kw} no GTA 6: {name} não esperava por isso"],
    "transcript": ["{name}: \"{quote}\"", "\"{quote}\" — {name} no GTA 6"],
}

DESCRIPTION_TEMPLATE = (
    "{title}\n\n"
    "Clipe de {name} — {link}\n"
    "Siga o canal {profile} para mais cortes de GTA 6.\n\n"
    "{hashtags}"
)


def _clean_quote(text: str, max_len: int = 48) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .,!?")
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return cut + "..."


def _pick_quote(transcript: str, rng: random.Random) -> str | None:
    if not transcript:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", transcript)
    sentences = [s for s in sentences if 12 <= len(s) <= 70]
    if sentences:
        return _clean_quote(rng.choice(sentences))
    return _clean_quote(transcript) if len(transcript) >= 12 else None


def _hashtags(cfg: Config, streamer: Streamer, network: str, rng: random.Random) -> list[str]:
    bank = cfg.hashtags
    limit = int(bank.get("limits", {}).get(network, 5))
    big = list(bank.get("big", []))
    mid = list(bank.get("mid", []))
    niche = list(bank.get("niche", [])) + list(bank.get("streamers", {}).get(streamer.handle, []))
    rng.shuffle(big), rng.shuffle(mid), rng.shuffle(niche)
    n_big = min(len(big), 2 if limit <= 6 else 3)
    n_niche = min(len(niche), 1 if limit <= 5 else 3)
    n_mid = max(0, limit - n_big - n_niche)
    tags = ["gta6"] + [t for t in big if t != "gta6"][: n_big - 1] + mid[:n_mid] + niche[:n_niche]
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return ["#" + t for t in seen[:limit]]


def generate_copy(cfg: Config, streamer: Streamer, moment: dict, llm_title: str | None = None) -> dict:
    """Retorna dict com titles (3), e por rede: title, description, hashtags."""
    rng = random.Random(moment["id"])
    reasons = json.loads(moment["reasons"]) if isinstance(moment.get("reasons"), str) else (moment.get("reasons") or {})
    transcript = moment.get("transcript") or ""
    name = streamer.name
    kw = (reasons.get("keywords_found") or [None])[0]
    quote = _pick_quote(transcript, rng)
    max_chars = int(cfg.settings.get("copy.title_max_chars", 70))

    candidates: list[str] = []
    if llm_title:
        candidates.append(llm_title)
    if kw:
        candidates += [h.format(name=name, kw=kw) for h in HOOKS_BY_REASON["keywords"]]
    if quote:
        candidates += [h.format(name=name, quote=quote) for h in HOOKS_BY_REASON["transcript"]]
    if (reasons.get("chat") or 0) >= 0.5:
        candidates += [h.format(name=name) for h in HOOKS_BY_REASON["chat"]]
    if (reasons.get("audio") or 0) >= 0.5:
        candidates += [h.format(name=name) for h in HOOKS_BY_REASON["audio"]]
    generic = [h.format(name=name) for h in HOOKS_GENERIC]
    rng.shuffle(generic)
    candidates += generic
    titles: list[str] = []
    for c in candidates:
        c = c.strip()[:max_chars]
        if c and c not in titles:
            titles.append(c)
        if len(titles) == 3:
            break

    profile = cfg.settings.get("copy.profile_handle", "@cortesgta6")
    networks: dict[str, dict] = {}
    for i, net in enumerate(NETWORKS):
        tags = _hashtags(cfg, streamer, net, random.Random(moment["id"] * 10 + i))
        title = titles[i % len(titles)]
        desc = DESCRIPTION_TEMPLATE.format(title=title, name=name, link=streamer.channel_link, profile=profile,
                                           hashtags=" ".join(tags))
        if net == "youtube":
            title_net = f"{title} #shorts"[:100]
        else:
            title_net = title
        networks[net] = {"title": title_net, "description": desc, "hashtags": tags}
    return {
        "titles": titles,
        "credit": f"Clipe de {name} — {streamer.channel_link}",
        "networks": networks,
        "music_note": ("YouTube: use apenas faixas de assets/music_free (livres de direitos). "
                       "TikTok/Instagram: adicione o som trending dentro do app na hora de postar."),
    }


def write_post_files(out_dir: Path, copy_data: dict, moment: dict, streamer: Streamer, clip_name: str) -> Path:
    post_json = out_dir / "post.json"
    post_json.write_text(json.dumps({"moment_id": moment["id"], "score": moment["score"], "streamer": streamer.handle,
                                     **copy_data}, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# {clip_name}", "",
          f"**Streamer:** {streamer.name} ({streamer.channel_link})  ",
          f"**Nota:** {moment['score']:.0f}/100  ",
          f"**Motivo:** {'; '.join((json.loads(moment['reasons']) if isinstance(moment['reasons'], str) else moment['reasons']).get('notes', []))}  ",
          f"**Transcrição:** {moment.get('transcript') or '(sem fala)'}", "",
          "## Títulos (escolha um)", *[f"{i + 1}. {t}" for i, t in enumerate(copy_data["titles"])], ""]
    for net, data in copy_data["networks"].items():
        md += [f"## {net.capitalize()}", "", f"**Título:** {data['title']}", "", "```", data["description"], "```", ""]
    md += ["## Música", copy_data["music_note"], ""]
    post_md = out_dir / "post.md"
    post_md.write_text("\n".join(md), encoding="utf-8")
    return post_md
