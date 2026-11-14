"""Gravação de chat com timestamps (Twitch IRC anônimo, Kick via Pusher, YouTube via chat-downloader).

Cada cliente roda numa thread, acumula mensagens e grava em lote no banco (chat_messages).
ts = instante de chegada (epoch). O alinhamento com o vídeo usa capture.chat_offset_seconds.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Iterable

from clipador.config import Streamer
from clipador.store.db import Database

log = logging.getLogger("capture.chat")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


class ChatRecorder(threading.Thread):
    """Classe base: subclasses implementam `_iter_messages()` -> yield (username, text)."""

    def __init__(self, db: Database, stream_id: int, streamer: Streamer):
        super().__init__(name=f"chat-{streamer.handle}", daemon=True)
        self.db = db
        self.stream_id = stream_id
        self.streamer = streamer
        self.stop_event = threading.Event()
        self._buffer: list[tuple[int, float, str | None, str]] = []
        self._lock = threading.Lock()
        self.count = 0
        self.connected = False

    def run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            try:
                for username, text in self._iter_messages():
                    if self.stop_event.is_set():
                        break
                    self._push(username, text)
                backoff = 5
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                log.warning("[%s] chat caiu: %s; reconectando em %ds", self.streamer.handle, str(exc)[:160], backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 120)
            finally:
                self.flush()
        self.flush()

    def _push(self, username: str | None, text: str) -> None:
        with self._lock:
            self._buffer.append((self.stream_id, time.time(), username, text[:500]))
            self.count += 1
            if len(self._buffer) >= 50:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._buffer:
            try:
                self.db.add_chat_messages(self._buffer)
            except Exception:  # noqa: BLE001
                log.exception("falha ao gravar chat")
            self._buffer = []

    def stop(self) -> None:
        self.stop_event.set()

    def _iter_messages(self) -> Iterable[tuple[str | None, str]]:  # pragma: no cover - abstrato
        raise NotImplementedError


class TwitchChat(ChatRecorder):
    HOST, PORT = "irc.chat.twitch.tv", 6667

    def _iter_messages(self):
        channel = self.streamer.url.rstrip("/").split("/")[-1].lower()
        sock = socket.create_connection((self.HOST, self.PORT), timeout=30)
        sock.settimeout(400)
        sock.sendall(b"NICK justinfan31337\r\n")
        sock.sendall(f"JOIN #{channel}\r\n".encode())
        self.connected = True
        log.info("[%s] chat Twitch conectado (#%s)", self.streamer.handle, channel)
        buf = b""
        try:
            while not self.stop_event.is_set():
                data = sock.recv(8192)
                if not data:
                    raise ConnectionError("conexão IRC fechada")
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    text = line.decode("utf-8", "replace")
                    if text.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        continue
                    if " PRIVMSG #" in text:
                        prefix, _, msg = text.partition(" PRIVMSG #")
                        user = prefix[1:].split("!", 1)[0]
                        _, _, body = msg.partition(" :")
                        yield user, body
        finally:
            try:
                sock.close()
            except OSError:
                pass


class KickChat(ChatRecorder):
    PUSHER_URL = ("wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js"
                  "&version=8.4.0-rc2&flash=false")

    def _chatroom_id(self) -> int:
        import httpx

        slug = self.streamer.url.rstrip("/").split("/")[-1].lower()
        headers = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "pt-BR,pt;q=0.9"}
        last_err = None
        for endpoint in (f"https://kick.com/api/v2/channels/{slug}", f"https://kick.com/api/v1/channels/{slug}"):
            try:
                with httpx.Client(http2=False, headers=headers, timeout=20, follow_redirects=True) as client:
                    r = client.get(endpoint)
                if r.status_code == 200:
                    data = r.json()
                    return int(data["chatroom"]["id"])
                last_err = f"HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:120]
        raise RuntimeError(f"não consegui obter chatroom id da Kick ({last_err}). "
                           "Kick usa proteção Cloudflare; a detecção segue sem chat para este streamer.")

    def _iter_messages(self):
        from websockets.sync.client import connect

        chatroom = self._chatroom_id()
        with connect(self.PUSHER_URL, open_timeout=20, additional_headers={"User-Agent": UA}) as ws:
            ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{chatroom}.v2"}}))
            self.connected = True
            log.info("[%s] chat Kick conectado (chatroom %d)", self.streamer.handle, chatroom)
            while not self.stop_event.is_set():
                try:
                    raw = ws.recv(timeout=60)
                except TimeoutError:
                    ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                name = evt.get("event", "")
                if name == "pusher:ping":
                    ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                elif name.endswith("ChatMessageEvent"):
                    data = evt.get("data")
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                    content = (data or {}).get("content") or ""
                    sender = ((data or {}).get("sender") or {}).get("username")
                    if content:
                        yield sender, content


class YouTubeChat(ChatRecorder):
    def _iter_messages(self):
        from chat_downloader import ChatDownloader

        chat = ChatDownloader().get_chat(self.streamer.url, message_groups=["messages"])
        self.connected = True
        log.info("[%s] chat YouTube conectado", self.streamer.handle)
        for msg in chat:
            if self.stop_event.is_set():
                break
            text = msg.get("message") or ""
            author = (msg.get("author") or {}).get("name")
            if text:
                yield author, text


def make_chat_recorder(db: Database, stream_id: int, streamer: Streamer) -> ChatRecorder:
    cls = {"twitch": TwitchChat, "kick": KickChat, "youtube": YouTubeChat}[streamer.platform]
    return cls(db, stream_id, streamer)
