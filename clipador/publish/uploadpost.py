"""Upload-Post (upload-post.com): um serviço com uma chave só que posta em YouTube/TikTok/Instagram/Kwai etc.

Alternativa quando não se quer criar apps nas APIs oficiais (ou para reaproveitar a ferramenta de terceiros
que o amigo do dono usa, se for essa). .env: UPLOAD_POST_API_KEY; settings: publish.upload_post.user (nome do
perfil criado no painel deles). Plano gratuito não posta no TikTok.
"""
from __future__ import annotations

import logging

from clipador.publish.base import PostPayload, PublishError, Publisher, PublishResult, env, error_text

log = logging.getLogger("publish.uploadpost")

API = "https://api.upload-post.com/api/upload"
PLATFORM_MAP = {"youtube": "youtube", "tiktok": "tiktok", "instagram": "instagram", "kwai": "kwai"}


class UploadPostPublisher(Publisher):
    """Uma instância por rede (name = rede), todas falando com a mesma API."""

    def __init__(self, network: str, cfg_net: dict, cfg_all: dict, tokens_dir):
        super().__init__(cfg_net, cfg_all, tokens_dir)
        self.name = network
        self.platform = PLATFORM_MAP.get(network, network)

    def configured(self) -> tuple[bool, str]:
        if not env("UPLOAD_POST_API_KEY"):
            return False, "falta UPLOAD_POST_API_KEY no .env"
        if not (self.all.get("upload_post") or {}).get("user"):
            return False, "defina publish.upload_post.user em settings.yaml (perfil do Upload-Post)"
        return True, "ok (via Upload-Post)"

    def publish(self, payload: PostPayload) -> PublishResult:
        user = (self.all.get("upload_post") or {}).get("user")
        data: dict = {"user": user, "platform[]": self.platform, "title": payload.title[:100]}
        if self.platform == "youtube":
            data.update({"youtube_title": payload.title[:100], "youtube_description": payload.caption[:4900],
                         "privacyStatus": self.cfg.get("privacy", "public"), "categoryId": str(self.cfg.get("category_id", "20"))})
        elif self.platform == "tiktok":
            data.update({"tiktok_title": payload.caption[:2200], "privacy_level": self.cfg.get("privacy_level", "PUBLIC_TO_EVERYONE"),
                         "post_mode": "DIRECT_POST"})
        elif self.platform == "instagram":
            data.update({"instagram_title": payload.caption[:2200], "media_type": "REELS"})
        else:
            data.update({"description": payload.caption[:2200]})
        headers = {"Authorization": f"Apikey {env('UPLOAD_POST_API_KEY')}",
                   "Idempotency-Key": f"clipador-{payload.clip_id}-{self.platform}-{payload.post_id}"}
        if self.platform == "youtube" and payload.tags:
            data["tags[]"] = payload.tags[:15]          # lista = campo repetido no multipart
        with self.http(timeout=600) as client, payload.video.open("rb") as f:
            files = {"video": (payload.video.name, f, "video/mp4")}
            r = client.post(API, headers=headers, data=data, files=files)
        j = r.json() if r.content else {}
        if r.status_code == 429:
            raise PublishError(f"Upload-Post: limite mensal atingido ({j.get('usage')})", retryable=False, code="quota")
        if r.status_code in (401, 403, 404):
            raise PublishError(f"Upload-Post: {error_text(r)}", retryable=False, code=str(r.status_code))
        if r.status_code >= 400:
            raise PublishError(f"Upload-Post HTTP {r.status_code}: {error_text(r)}", retryable=r.status_code >= 500)
        if j.get("request_id") and not j.get("results"):
            return PublishResult(url=None, remote_id=str(j["request_id"]), status="processing", raw=j)
        res = (j.get("results") or {}).get(self.platform) or {}
        if not res.get("success", j.get("success")):
            raise PublishError(f"Upload-Post {self.platform}: {res.get('error') or j}", retryable=False)
        rid = res.get("post_id") or res.get("video_id") or res.get("id")
        return PublishResult(url=res.get("url"), remote_id=str(rid) if rid else None, status="posted", raw=j)
