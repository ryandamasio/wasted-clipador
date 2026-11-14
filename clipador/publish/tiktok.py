"""TikTok Content Posting API (Direct Post, FILE_UPLOAD em chunks).

.env: TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET (app no TikTok for Developers com Content Posting API + Login Kit,
      escopos user.info.basic, video.publish). TIKTOK_ACCESS_TOKEN/TIKTOK_REFRESH_TOKEN iniciais vêm de
      `clipador publish auth tiktok` (fluxo web) e são renovados/rotacionados em data/tokens/tiktok.json.
Limites: app não auditado só posta em modo privado (SELF_ONLY) e em contas privadas; 6 inits/min, 15 posts/dia.
"""
from __future__ import annotations

import logging
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from clipador.publish.base import PostPayload, PublishError, Publisher, PublishResult, env, error_text

log = logging.getLogger("publish.tiktok")

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
CHUNK = 10 * 1024 * 1024          # 5-64 MB; vídeos < 5 MB vão num chunk só


class TikTokPublisher(Publisher):
    name = "tiktok"

    def configured(self) -> tuple[bool, str]:
        if not (env("TIKTOK_CLIENT_KEY") and env("TIKTOK_CLIENT_SECRET")):
            return False, "faltam TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET no .env"
        if not (self.tokens.get("refresh_token", "TIKTOK_REFRESH_TOKEN") or self.tokens.get("access_token", "TIKTOK_ACCESS_TOKEN")):
            return False, "sem token: rode `clipador publish auth tiktok`"
        return True, "ok"

    # ------------------------------------------------------------------ auth
    def access_token(self, client: httpx.Client) -> str:
        tok = self.tokens.load()
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 120:
            return tok["access_token"]
        refresh = self.tokens.get("refresh_token", "TIKTOK_REFRESH_TOKEN")
        if not refresh:
            at = self.tokens.get("access_token", "TIKTOK_ACCESS_TOKEN")
            if at:
                return at
            raise PublishError("TikTok sem token", retryable=False, code="no_token")
        r = client.post(f"{API}/oauth/token/", data={"client_key": env("TIKTOK_CLIENT_KEY"), "client_secret": env("TIKTOK_CLIENT_SECRET"),
                                                      "grant_type": "refresh_token", "refresh_token": refresh},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = r.json() if r.content else {}
        if r.status_code != 200 or not j.get("access_token"):
            raise PublishError(f"TikTok token: {error_text(r)}", retryable=r.status_code >= 500, code="token")
        self.tokens.save(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in", 86400)),
                         refresh_token=j.get("refresh_token", refresh), open_id=j.get("open_id"))
        return j["access_token"]

    def auth_flow(self, redirect_uri: str | None = None, port: int = 8791, open_browser: bool = True) -> str:
        """Login Kit: a URL de redirect precisa estar cadastrada no app TikTok (ex.: https://SEU-DOMINIO/callback).

        Sem domínio público, cole manualmente o `code` da URL de retorno quando pedido.
        """
        key, sec = env("TIKTOK_CLIENT_KEY"), env("TIKTOK_CLIENT_SECRET")
        if not (key and sec):
            raise PublishError("defina TIKTOK_CLIENT_KEY e TIKTOK_CLIENT_SECRET no .env", retryable=False)
        redirect = redirect_uri or self.cfg.get("redirect_uri") or f"http://127.0.0.1:{port}/callback"
        state = secrets.token_urlsafe(12)
        url = AUTH_URL + "?" + urllib.parse.urlencode({"client_key": key, "response_type": "code", "scope": "user.info.basic,video.publish",
                                                       "redirect_uri": redirect, "state": state})
        print("Abra no navegador e autorize:\n" + url)
        if open_browser:
            webbrowser.open(url)
        code = None
        if redirect.startswith("http://127.0.0.1") or redirect.startswith("http://localhost"):
            box: dict = {}

            class H(BaseHTTPRequestHandler):
                def do_GET(self):  # noqa: N802
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    if q.get("code"):
                        box["code"] = q["code"][0]
                    self.send_response(200); self.end_headers(); self.wfile.write(b"<h2>Pode fechar esta aba.</h2>")

                def log_message(self, *a):
                    pass

            srv = HTTPServer(("127.0.0.1", port), H)
            while "code" not in box:
                srv.handle_request()
            srv.server_close()
            code = box["code"]
        else:
            code = input("Cole aqui o valor de `code` da URL para onde o TikTok redirecionou: ").strip()
        with self.http() as client:
            r = client.post(f"{API}/oauth/token/", data={"client_key": key, "client_secret": sec, "code": code,
                                                          "grant_type": "authorization_code", "redirect_uri": redirect},
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        j = r.json() if r.content else {}
        if r.status_code != 200 or not j.get("access_token"):
            raise PublishError(f"TikTok auth falhou: {error_text(r)}", retryable=False)
        self.tokens.save(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in", 86400)),
                         refresh_token=j.get("refresh_token"), open_id=j.get("open_id"))
        return j["access_token"]

    # ------------------------------------------------------------------ publish
    def publish(self, payload: PostPayload) -> PublishResult:
        size = payload.video.stat().st_size
        chunk = CHUNK if size > CHUNK else size
        total_chunks = max(1, size // chunk)          # TikTok: último chunk pode ser maior que os outros
        privacy = self.cfg.get("privacy_level", "SELF_ONLY")
        with self.http(timeout=300) as client:
            token = self.access_token(client)
            h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
            # opções de privacidade da conta (evita privacy_level_option_mismatch)
            ci = client.post(f"{API}/post/publish/creator_info/query/", headers=h)
            if ci.status_code == 200:
                opts = (ci.json().get("data") or {}).get("privacy_level_options") or []
                if opts and privacy not in opts:
                    log.warning("TikTok: privacy_level %s não disponível (%s); usando %s", privacy, opts, opts[0])
                    privacy = opts[0]
            body = {
                "post_info": {"title": payload.caption[:2200], "privacy_level": privacy,
                              "disable_duet": bool(self.cfg.get("disable_duet", False)), "disable_comment": bool(self.cfg.get("disable_comment", False)),
                              "disable_stitch": bool(self.cfg.get("disable_stitch", False)), "video_cover_timestamp_ms": int(self.cfg.get("cover_ms", 1000))},
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total_chunks},
            }
            r = client.post(f"{API}/post/publish/video/init/", headers=h, json=body)
            j = r.json() if r.content else {}
            code = (j.get("error") or {}).get("code", "")
            if r.status_code != 200 or code not in ("ok", ""):
                fatal = code in ("unaudited_client_can_only_post_to_private_accounts", "privacy_level_option_mismatch", "access_token_invalid",
                                 "scope_not_authorized", "invalid_param")
                raise PublishError(f"TikTok init: {code or r.status_code} {(j.get('error') or {}).get('message', '')}", retryable=not fatal, code=code)
            publish_id, upload_url = j["data"]["publish_id"], j["data"].get("upload_url")
            with payload.video.open("rb") as f:
                offset = 0
                for i in range(total_chunks):
                    n = size - offset if i == total_chunks - 1 else chunk
                    data = f.read(n)
                    end = offset + len(data) - 1
                    up = client.put(upload_url, content=data, headers={"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                                                                        "Content-Range": f"bytes {offset}-{end}/{size}"})
                    if up.status_code not in (200, 201, 206):
                        raise PublishError(f"TikTok upload chunk {i}: HTTP {up.status_code} {up.text[:200]}")
                    offset = end + 1
            # aguarda processamento (curto); o resto fica para fetch_metrics/status depois
            status, raw = "processing", {}
            for _ in range(int(self.cfg.get("status_polls", 10))):
                time.sleep(float(self.cfg.get("status_poll_seconds", 3)))
                st = client.post(f"{API}/post/publish/status/fetch/", headers=h, json={"publish_id": publish_id})
                raw = st.json() if st.content else {}
                s = (raw.get("data") or {}).get("status", "")
                if s == "PUBLISH_COMPLETE":
                    status = "posted"
                    break
                if s == "FAILED":
                    raise PublishError(f"TikTok falhou: {(raw.get('data') or {}).get('fail_reason', '?')}", retryable=False, code="failed")
        ids = (raw.get("data") or {}).get("publicaly_available_post_id") or []
        url = f"https://www.tiktok.com/@/video/{ids[0]}" if ids else None
        return PublishResult(url=url, remote_id=publish_id, status=status, raw=raw)

    def fetch_metrics(self, remote_id: str) -> dict | None:
        with self.http() as client:
            token = self.access_token(client)
            st = client.post(f"{API}/post/publish/status/fetch/", headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                             json={"publish_id": remote_id})
            if st.status_code != 200:
                return None
            d = st.json().get("data") or {}
            return {"status": d.get("status"), "post_ids": d.get("publicaly_available_post_id")}
