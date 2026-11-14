"""YouTube Data API v3 (Shorts = vídeo vertical normal). OAuth2 "installed app" com refresh token.

.env: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET (app OAuth do tipo Desktop no Google Cloud, API YouTube Data v3 ativada)
      YOUTUBE_REFRESH_TOKEN (gerado por `clipador publish auth youtube`; também fica em data/tokens/youtube.json)
Quota: upload custa 1600 unidades de 10.000/dia -> no máximo 6 uploads/dia por projeto Google.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from clipador.publish.base import PostPayload, PublishError, Publisher, PublishResult, env, error_text

log = logging.getLogger("publish.youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024


class YouTubePublisher(Publisher):
    name = "youtube"

    def configured(self) -> tuple[bool, str]:
        if not (env("YOUTUBE_CLIENT_ID") and env("YOUTUBE_CLIENT_SECRET")):
            return False, "faltam YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET no .env"
        if not self.tokens.get("refresh_token", "YOUTUBE_REFRESH_TOKEN"):
            return False, "sem refresh token: rode `clipador publish auth youtube`"
        return True, "ok"

    # ------------------------------------------------------------------ auth
    def access_token(self, client: httpx.Client) -> str:
        tok = self.tokens.load()
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]
        refresh = self.tokens.get("refresh_token", "YOUTUBE_REFRESH_TOKEN")
        if not refresh:
            raise PublishError("YouTube sem refresh token", retryable=False, code="no_token")
        r = client.post(TOKEN_URL, data={"client_id": env("YOUTUBE_CLIENT_ID"), "client_secret": env("YOUTUBE_CLIENT_SECRET"),
                                         "refresh_token": refresh, "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise PublishError(f"YouTube token: {error_text(r)}", retryable=r.status_code >= 500, code="token")
        j = r.json()
        self.tokens.save(access_token=j["access_token"], expires_at=time.time() + int(j.get("expires_in", 3600)))
        return j["access_token"]

    def auth_flow(self, port: int = 8790, open_browser: bool = True) -> str:
        """Fluxo OAuth de app instalado: abre o navegador, recebe o code no loopback e troca por refresh token."""
        cid, csec = env("YOUTUBE_CLIENT_ID"), env("YOUTUBE_CLIENT_SECRET")
        if not (cid and csec):
            raise PublishError("defina YOUTUBE_CLIENT_ID e YOUTUBE_CLIENT_SECRET no .env primeiro", retryable=False)
        redirect = f"http://127.0.0.1:{port}/"
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({"client_id": cid, "redirect_uri": redirect, "response_type": "code",
                                                       "scope": SCOPES, "access_type": "offline", "prompt": "consent", "state": state})
        code_box: dict = {}

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if q.get("state", [""])[0] == state and q.get("code"):
                    code_box["code"] = q["code"][0]
                    msg = "Autorizado! Pode fechar esta aba e voltar ao terminal."
                else:
                    msg = "Faltou o código de autorização. Tente de novo."
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"<h2>{msg}</h2>".encode("utf-8"))

            def log_message(self, *a):  # silencioso
                pass

        srv = HTTPServer(("127.0.0.1", port), H)
        print("Abra no navegador (se não abrir sozinho):\n" + url)
        if open_browser:
            webbrowser.open(url)
        while "code" not in code_box:
            srv.handle_request()
        srv.server_close()
        with self.http() as client:
            r = client.post(TOKEN_URL, data={"code": code_box["code"], "client_id": cid, "client_secret": csec,
                                             "redirect_uri": redirect, "grant_type": "authorization_code"})
        if r.status_code != 200:
            raise PublishError(f"troca do code falhou: {error_text(r)}", retryable=False)
        j = r.json()
        if not j.get("refresh_token"):
            raise PublishError("Google não devolveu refresh_token (revogue o acesso em myaccount.google.com/permissions e repita)", retryable=False)
        self.tokens.save(refresh_token=j["refresh_token"], access_token=j.get("access_token"),
                         expires_at=time.time() + int(j.get("expires_in", 3600)))
        return j["refresh_token"]

    # ------------------------------------------------------------------ publish
    def publish(self, payload: PostPayload) -> PublishResult:
        size = payload.video.stat().st_size
        body = {
            "snippet": {"title": payload.title[:100], "description": payload.caption[:4900], "tags": payload.tags[:30],
                        "categoryId": str(self.cfg.get("category_id", "20")), "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"},
            "status": {"privacyStatus": self.cfg.get("privacy", "public"), "selfDeclaredMadeForKids": bool(self.cfg.get("made_for_kids", False)),
                       "embeddable": True},
        }
        with self.http(timeout=300) as client:
            token = self.access_token(client)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                       "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}
            r = client.post(UPLOAD_URL, headers=headers, content=json.dumps(body))
            if r.status_code != 200:
                self._raise(r, "início do upload")
            session = r.headers.get("Location")
            if not session:
                raise PublishError("YouTube não devolveu URL de sessão de upload")
            with payload.video.open("rb") as f:
                offset = 0
                while offset < size:
                    chunk = f.read(CHUNK)
                    end = offset + len(chunk) - 1
                    h = {"Authorization": f"Bearer {token}", "Content-Length": str(len(chunk)), "Content-Type": "video/mp4",
                         "Content-Range": f"bytes {offset}-{end}/{size}"}
                    r = client.put(session, headers=h, content=chunk)
                    if r.status_code in (200, 201):
                        break
                    if r.status_code == 308:
                        rng = r.headers.get("Range")
                        offset = int(rng.split("-")[1]) + 1 if rng else end + 1
                        continue
                    self._raise(r, f"upload (byte {offset})")
            if r.status_code not in (200, 201):
                raise PublishError(f"upload terminou com HTTP {r.status_code}")
            j = r.json()
            vid = j.get("id")
            if not vid:
                raise PublishError(f"resposta sem id: {str(j)[:200]}", retryable=False)
            status = j.get("status", {}).get("uploadStatus", "uploaded")
            if payload.cover and payload.cover.exists() and self.cfg.get("set_thumbnail", False):
                try:
                    client.post(f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={vid}",
                                headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}, content=payload.cover.read_bytes())
                except Exception as exc:  # noqa: BLE001
                    log.warning("thumbnail não aplicada: %s", exc)
        return PublishResult(url=f"https://www.youtube.com/shorts/{vid}", remote_id=vid,
                             status="posted" if status in ("uploaded", "processed") else "processing", raw=j)

    def fetch_metrics(self, remote_id: str) -> dict | None:
        with self.http() as client:
            token = self.access_token(client)
            r = client.get(VIDEOS_URL, params={"part": "statistics,status", "id": remote_id}, headers={"Authorization": f"Bearer {token}"})
            if r.status_code != 200:
                return None
            items = r.json().get("items") or []
            if not items:
                return None
            st = items[0].get("statistics", {})
            return {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)), "comments": int(st.get("commentCount", 0)),
                    "status": items[0].get("status", {}).get("uploadStatus")}

    @staticmethod
    def _raise(r: httpx.Response, where: str) -> None:
        msg = error_text(r)
        if r.status_code in (401, 403):
            retry = "quotaExceeded" in r.text or "rateLimit" in r.text
            raise PublishError(f"YouTube {where}: {msg}", retryable=retry, code="quota" if retry else "auth")
        raise PublishError(f"YouTube {where}: HTTP {r.status_code} {msg}", retryable=r.status_code >= 500 or r.status_code == 429)
