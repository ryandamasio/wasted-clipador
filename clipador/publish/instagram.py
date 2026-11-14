"""Instagram Graph API: Reels com upload resumível (sem precisar hospedar o vídeo numa URL pública).

.env: INSTAGRAM_ACCESS_TOKEN (token de usuário de longa duração com instagram_business_content_publish,
      instagram_business_basic, pages_read_engagement; conta profissional ligada a uma Página)
      INSTAGRAM_USER_ID (ID da conta profissional do Instagram, não o @)
Limite da API: 100 publicações por 24 h por conta. Vídeo: MP4 H.264/AAC, 9:16, 3 s-15 min, <= 300 MB.
"""
from __future__ import annotations

import logging
import time

from clipador.publish.base import PostPayload, PublishError, Publisher, PublishResult, env, error_text

log = logging.getLogger("publish.instagram")

GRAPH = "https://graph.facebook.com/v21.0"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"


class InstagramPublisher(Publisher):
    name = "instagram"

    def configured(self) -> tuple[bool, str]:
        if not (env("INSTAGRAM_ACCESS_TOKEN") and env("INSTAGRAM_USER_ID")):
            return False, "faltam INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_USER_ID no .env"
        return True, "ok"

    def publish(self, payload: PostPayload) -> PublishResult:
        token, uid = env("INSTAGRAM_ACCESS_TOKEN"), env("INSTAGRAM_USER_ID")
        size = payload.video.stat().st_size
        with self.http(timeout=600) as client:
            r = client.post(f"{GRAPH}/{uid}/media", data={"media_type": "REELS", "upload_type": "resumable", "caption": payload.caption[:2200],
                                                           "share_to_feed": "true" if self.cfg.get("share_to_feed", True) else "false",
                                                           "access_token": token})
            if r.status_code != 200:
                self._raise(r, "criar container")
            j = r.json()
            container = j.get("id")
            uri = j.get("uri") or f"{RUPLOAD}/{container}"
            with payload.video.open("rb") as f:
                up = client.post(uri, headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}, content=f.read())
            if up.status_code != 200 or not (up.json() if up.content else {}).get("success", True):
                raise PublishError(f"Instagram upload: HTTP {up.status_code} {up.text[:200]}")
            # processamento
            status = "IN_PROGRESS"
            for _ in range(int(self.cfg.get("status_polls", 30))):
                time.sleep(float(self.cfg.get("status_poll_seconds", 5)))
                st = client.get(f"{GRAPH}/{container}", params={"fields": "status_code,status", "access_token": token})
                sj = st.json() if st.content else {}
                status = sj.get("status_code", status)
                if status == "FINISHED":
                    break
                if status in ("ERROR", "EXPIRED"):
                    raise PublishError(f"Instagram processamento: {sj.get('status', status)}", retryable=False, code=status)
            if status != "FINISHED":
                raise PublishError("Instagram ainda processando após a espera; tentará de novo", retryable=True, code="timeout")
            pub = client.post(f"{GRAPH}/{uid}/media_publish", data={"creation_id": container, "access_token": token})
            if pub.status_code != 200:
                self._raise(pub, "publicar")
            media_id = pub.json().get("id")
            permalink = None
            try:
                pl = client.get(f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token})
                permalink = pl.json().get("permalink") if pl.status_code == 200 else None
            except Exception:  # noqa: BLE001
                pass
        return PublishResult(url=permalink, remote_id=media_id, status="posted", raw={"container": container})

    def fetch_metrics(self, remote_id: str) -> dict | None:
        token = env("INSTAGRAM_ACCESS_TOKEN")
        with self.http() as client:
            r = client.get(f"{GRAPH}/{remote_id}", params={"fields": "like_count,comments_count,permalink", "access_token": token})
            if r.status_code != 200:
                return None
            j = r.json()
            out = {"likes": j.get("like_count"), "comments": j.get("comments_count"), "url": j.get("permalink")}
            ins = client.get(f"{GRAPH}/{remote_id}/insights", params={"metric": "plays,reach,shares,saved", "access_token": token})
            if ins.status_code == 200:
                for m in ins.json().get("data", []):
                    vals = m.get("values") or [{}]
                    out[m.get("name")] = vals[0].get("value")
            return out

    @staticmethod
    def _raise(r, where: str) -> None:
        msg = error_text(r)
        code = ""
        try:
            code = str((r.json().get("error") or {}).get("code", ""))
        except Exception:  # noqa: BLE001
            pass
        fatal = code in ("190", "10", "200", "100")   # token inválido / permissão / parâmetro
        raise PublishError(f"Instagram {where}: {msg}", retryable=not fatal and r.status_code != 400, code=code or str(r.status_code))
