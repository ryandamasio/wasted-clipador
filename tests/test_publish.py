import json
import random
import time
from datetime import datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from clipador.config import load_config
from clipador.publish import base
from clipador.publish.base import PostPayload, PublishError, Publisher, PublishResult
from clipador.publish.instagram import InstagramPublisher
from clipador.publish.manual import ManualPublisher
from clipador.publish.scheduler import Scheduler, build_publishers, parse_windows
from clipador.publish.tiktok import TikTokPublisher
from clipador.publish.uploadpost import UploadPostPublisher
from clipador.publish.youtube import YouTubePublisher
from clipador.review.app import create_app
from clipador.review.service import ReviewService
from clipador.store.db import Database


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi).timestamp()


class FakePublisher(Publisher):
    def __init__(self, name, outcome="ok"):
        super().__init__({}, {}, Path("."))
        self.name = name
        self.outcome = outcome
        self.calls = []

    def configured(self):
        return True, "ok"

    def publish(self, payload: PostPayload) -> PublishResult:
        self.calls.append(payload)
        if self.outcome == "retry":
            raise PublishError("instável", retryable=True)
        if self.outcome == "fatal":
            raise PublishError("credencial", retryable=False)
        return PublishResult(url=f"https://x/{self.name}/{payload.post_id}", remote_id=f"r{payload.post_id}")


def _seed(tmp_path, n_clips=1):
    cfg = load_config()
    db = Database(tmp_path / "t.sqlite")
    sid = db.create_stream("coringa", "kick", "https://kick.com/coringa", tmp_path)
    seg = db.add_segment(sid, 0, tmp_path / "seg0.ts", 100.0, 220.0)
    ids = []
    for i in range(n_clips):
        mid = db.add_moment(sid, seg, 150.0 + i, 175.0 + i, 152.0, 80.0, {"notes": []}, "fala", [])
        d = tmp_path / f"clip{i}"
        d.mkdir()
        (d / "clip.mp4").write_bytes(b"\x00" * 50000)
        copy_data = {"titles": ["T1", "T2", "T3"], "credit": "c",
                     "networks": {n: {"title": f"T1 {n}", "description": f"desc {n}", "hashtags": ["#gta6", "#" + n]} for n in ("youtube", "tiktok", "instagram", "kwai")}}
        ids.append(db.add_clip(mid, d / "clip.mp4", None, None, 25.0, "T1", copy_data))
    return cfg, db, ids


def _sched(cfg, db, pubs=None, seed=1):
    return Scheduler(cfg, db, pubs if pubs is not None else {"youtube": FakePublisher("youtube"), "tiktok": FakePublisher("tiktok")},
                     rng=random.Random(seed))


# ------------------------------------------------------------------ janelas e vagas
def test_parse_windows_sorted():
    w = parse_windows(["20:30-23:00", "11:30-13:30"])
    assert (w[0].start_min, w[0].end_min, w[1].start_min) == (11 * 60 + 30, 13 * 60 + 30, 20 * 60 + 30)


def test_next_slot_respects_windows_gap_and_daily_limit(tmp_path):
    cfg, db, ids = _seed(tmp_path)
    sch = _sched(cfg, db)
    sch.p["windows"] = ["11:30-13:30", "20:30-23:00"]; sch.windows = parse_windows(sch.p["windows"])
    sch.p["min_gap_minutes"] = 90; sch.p["warmup_ramp"] = []; sch.p["posts_per_day"] = 2
    now = _ts(2026, 11, 20, 9, 0)
    s1 = sch.next_slot("youtube", now, now)
    assert datetime.fromtimestamp(s1).strftime("%H:%M") == "11:30"
    db.add_post(ids[0], "youtube", s1, "t", "c", "api")
    s2 = sch.next_slot("youtube", now, now)       # 90 min depois, ainda na janela
    assert datetime.fromtimestamp(s2).strftime("%H:%M") == "13:00"
    db.add_post(ids[0], "youtube", s2, "t", "c", "api")
    s3 = sch.next_slot("youtube", now, now)       # limite diário 2 -> amanhã
    assert datetime.fromtimestamp(s3).strftime("%d %H:%M") == "21 11:30"
    # fora das janelas, no meio da tarde -> noite
    s4 = sch.next_slot("tiktok", _ts(2026, 11, 20, 15, 0), now)
    assert datetime.fromtimestamp(s4).strftime("%H:%M") == "20:30"


def test_warmup_ramp_limits_first_days(tmp_path):
    cfg, db, ids = _seed(tmp_path)
    sch = _sched(cfg, db)
    sch.p["warmup_ramp"] = [1, 2]; sch.p["posts_per_day"] = 3
    day0 = _ts(2026, 11, 20, 0, 0)
    assert sch.daily_limit("youtube", day0) == 1          # nenhum post ainda: dia 0 da rampa
    db.add_post(ids[0], "youtube", _ts(2026, 11, 20, 12, 0), "t", "c", "api")
    assert sch.daily_limit("youtube", day0) == 1
    assert sch.daily_limit("youtube", day0 + 86400) == 2
    assert sch.daily_limit("youtube", day0 + 5 * 86400) == 3


def test_schedule_clip_staggers_networks_and_skips_existing(tmp_path):
    cfg, db, ids = _seed(tmp_path)
    sch = _sched(cfg, db)
    now = _ts(2026, 11, 20, 12, 0)
    created = sch.schedule_clip(ids[0], now=now)
    posts = db.posts_for_clip(ids[0])
    assert len(created) == 2 and {p["platform"] for p in posts} == {"youtube", "tiktok"}
    yt = next(p for p in posts if p["platform"] == "youtube"); tt = next(p for p in posts if p["platform"] == "tiktok")
    assert yt["title"] == "T1" and tt["title"] == "T1"   # #Shorts vai na descrição
    assert 3 * 60 <= yt["scheduled_at"] - now <= 12 * 60             # jitter
    assert 8 * 60 <= tt["scheduled_at"] - yt["scheduled_at"] <= 35 * 60   # defasagem entre redes
    assert sch.schedule_clip(ids[0], now=now) == []                   # idempotente
    assert sch.cancel_clip(ids[0]) == 2
    assert all(p["status"] == "skipped" for p in db.posts_for_clip(ids[0]))


# ------------------------------------------------------------------ execução
def test_run_due_success_retry_and_fatal(tmp_path):
    cfg, db, ids = _seed(tmp_path, 3)
    pubs = {"youtube": FakePublisher("youtube", "ok"), "tiktok": FakePublisher("tiktok", "retry"), "instagram": FakePublisher("instagram", "fatal"),
            "kwai": ManualPublisher({}, {}, tmp_path)}
    pubs["kwai"].name = "kwai"
    sch = _sched(cfg, db, pubs)
    past = time.time() - 60
    p_ok = db.add_post(ids[0], "youtube", past, "t", "c", "api")
    p_retry = db.add_post(ids[1], "tiktok", past, "t", "c", "api")
    p_fatal = db.add_post(ids[2], "instagram", past, "t", "c", "api")
    p_manual = db.add_post(ids[0], "kwai", past, "t", "c", "manual")
    res = {r["post_id"]: r["status"] for r in sch.run_due()}
    assert res == {p_ok: "posted", p_retry: "retry", p_fatal: "failed"}
    ok = db.get_post(p_ok)
    assert ok["status"] == "posted" and ok["url"].startswith("https://x/youtube/") and ok["remote_id"] == f"r{p_ok}"
    rt = db.get_post(p_retry)
    assert rt["status"] == "scheduled" and rt["attempts"] == 1 and 4 * 60 < rt["scheduled_at"] - time.time() <= 5 * 60
    assert db.get_post(p_fatal)["status"] == "failed"
    assert db.get_post(p_manual)["status"] == "scheduled"          # manual não é tocado
    # clipe 2 (só tiktok, que falhou de forma retryable) continua approved; clipe 1 virou posted? tem kwai manual pendente -> não
    assert db.get_clip(ids[0])["status"] == "ready"
    # esgota as tentativas
    sch.p["retry_backoff_minutes"] = [1]
    db.update("posts", p_retry, scheduled_at=past)
    assert sch.run_due()[0]["status"] == "failed"


def test_build_publishers_manual_mode_and_auto(tmp_path, monkeypatch):
    cfg = load_config()
    for net in ("instagram", "kwai"):  # desligadas na config real desde 19/11; o teste cobre as 4
        monkeypatch.setitem(cfg.settings._data["publish"]["networks"][net], "enabled", True)
    pubs = build_publishers(cfg)
    assert set(pubs) == {"youtube", "tiktok", "instagram", "kwai"} and all(p.manual for p in pubs.values())
    monkeypatch.setitem(cfg.settings._data["publish"], "mode", "auto")
    pubs = build_publishers(cfg)
    assert isinstance(pubs["youtube"], YouTubePublisher) and isinstance(pubs["tiktok"], TikTokPublisher)
    assert isinstance(pubs["instagram"], InstagramPublisher) and pubs["kwai"].manual
    ok, why = pubs["youtube"].configured()
    assert not ok and "YOUTUBE_CLIENT_ID" in why
    monkeypatch.setitem(cfg.settings._data["publish"]["networks"]["youtube"], "via", "upload-post")
    assert isinstance(build_publishers(cfg)["youtube"], UploadPostPublisher)
    monkeypatch.setitem(cfg.settings._data["publish"], "mode", "manual")


# ------------------------------------------------------------------ fila web + agenda
def test_review_approve_schedules_and_posts_api(tmp_path):
    cfg, db, ids = _seed(tmp_path)
    sch = _sched(cfg, db, {"youtube": ManualPublisher({}, {}, tmp_path), "tiktok": FakePublisher("tiktok")})
    sch.publishers["youtube"].name = "youtube"
    svc = ReviewService(cfg, db, sch)
    svc.approve(ids[0], title="Meu título")
    posts = db.posts_for_clip(ids[0])
    assert len(posts) == 2 and all(p["status"] == "scheduled" for p in posts)
    client = TestClient(create_app(cfg, db, sch))
    j = client.get("/api/posts").json()
    assert len(j["posts"]) == 2 and j["publish"]["networks"]["youtube"]["manual"] is True
    yt = next(p for p in j["posts"] if p["platform"] == "youtube"); tt = next(p for p in j["posts"] if p["platform"] == "tiktok")
    assert yt["publisher"] == "manual" and yt["title"] == "Meu título"
    # dono postou o do YouTube à mão
    r = client.post(f"/api/posts/{yt['id']}/posted", json={"url": "https://youtube.com/shorts/abc"})
    assert r.status_code == 200 and db.get_post(yt["id"])["url"].endswith("abc")
    # "postar agora" no tiktok (publisher fake) publica na hora
    r = client.post(f"/api/posts/{tt['id']}/now", json={})
    assert r.json()["post"]["status"] == "posted"
    assert db.get_clip(ids[0])["status"] == "posted"
    assert client.get("/api/posts?status=posted").json()["posts"][0]["url"]
    # rejeitar depois cancela o que estiver agendado
    svc.approve(ids[0]); svc.reject(ids[0], "x")
    assert all(p["status"] in ("posted", "skipped") for p in db.posts_for_clip(ids[0]))


# ------------------------------------------------------------------ publishers com HTTP simulado
def _payload(tmp_path):
    v = tmp_path / "v.mp4"
    v.write_bytes(b"v" * 3000)
    return PostPayload(video=v, title="Título", caption="desc #gta6", tags=["gta6"], clip_id=1, post_id=9)


def test_youtube_publish_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "id"); monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "s"); monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "rt")
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url)))
        if "oauth2.googleapis.com/token" in str(req.url):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        if "uploadType=resumable" in str(req.url):
            assert json.loads(req.content)["snippet"]["title"] == "Título"
            return httpx.Response(200, headers={"Location": "https://upload.example/session"})
        if "upload.example/session" in str(req.url):
            assert req.headers["Content-Range"] == "bytes 0-2999/3000"
            return httpx.Response(200, json={"id": "VID123", "status": {"uploadStatus": "uploaded"}})
        return httpx.Response(404)

    base.set_transport(httpx.MockTransport(handler))
    try:
        pub = YouTubePublisher({"privacy": "public"}, {}, tmp_path)
        assert pub.configured()[0]
        res = pub.publish(_payload(tmp_path))
    finally:
        base.set_transport(None)
    assert res.url == "https://www.youtube.com/shorts/VID123" and res.status == "posted"
    assert pub.tokens.load()["access_token"] == "AT"


def test_tiktok_publish_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k"); monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s"); monkeypatch.setenv("TIKTOK_REFRESH_TOKEN", "rt")
    polls = {"n": 0}

    def handler(req: httpx.Request):
        u = str(req.url)
        if u.endswith("/oauth/token/"):
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 86400, "refresh_token": "rt2", "open_id": "o"})
        if u.endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]}, "error": {"code": "ok"}})
        if u.endswith("/video/init/"):
            b = json.loads(req.content)
            assert b["post_info"]["privacy_level"] == "SELF_ONLY" and b["source_info"]["total_chunk_count"] == 1
            return httpx.Response(200, json={"data": {"publish_id": "P1", "upload_url": "https://up.tiktok/x?y=1"}, "error": {"code": "ok"}})
        if u.startswith("https://up.tiktok/"):
            assert req.headers["Content-Range"] == "bytes 0-2999/3000"
            return httpx.Response(201)
        if u.endswith("/status/fetch/"):
            polls["n"] += 1
            st = "PROCESSING_UPLOAD" if polls["n"] < 2 else "PUBLISH_COMPLETE"
            return httpx.Response(200, json={"data": {"status": st, "publicaly_available_post_id": [777] if st == "PUBLISH_COMPLETE" else []}})
        return httpx.Response(404)

    base.set_transport(httpx.MockTransport(handler))
    try:
        pub = TikTokPublisher({"privacy_level": "SELF_ONLY", "status_poll_seconds": 0}, {}, tmp_path)
        res = pub.publish(_payload(tmp_path))
    finally:
        base.set_transport(None)
    assert res.status == "posted" and res.remote_id == "P1" and "777" in res.url
    assert pub.tokens.load()["refresh_token"] == "rt2"      # rotação persistida


def test_tiktok_unaudited_error_is_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k"); monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "s"); monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "AT")

    def handler(req):
        if str(req.url).endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(403, json={"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "x"}})

    base.set_transport(httpx.MockTransport(handler))
    try:
        pub = TikTokPublisher({}, {}, tmp_path)
        try:
            pub.publish(_payload(tmp_path)); assert False
        except PublishError as exc:
            assert not exc.retryable
    finally:
        base.set_transport(None)


def test_instagram_publish_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "T"); monkeypatch.setenv("INSTAGRAM_USER_ID", "123")
    polls = {"n": 0}

    def handler(req: httpx.Request):
        path, q = req.url.path, str(req.url.query)
        if path.endswith("/123/media"):
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v21.0/C1"})
        if req.url.host == "rupload.facebook.com":
            assert req.headers["offset"] == "0" and req.headers["file_size"] == "3000" and req.headers["Authorization"] == "OAuth T"
            return httpx.Response(200, json={"success": True})
        if path.endswith("/C1") and "status_code" in q:
            polls["n"] += 1
            return httpx.Response(200, json={"status_code": "IN_PROGRESS" if polls["n"] < 2 else "FINISHED"})
        if path.endswith("/123/media_publish"):
            return httpx.Response(200, json={"id": "M9"})
        if path.endswith("/M9") and "permalink" in q:
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})
        return httpx.Response(404, json={"error": {"message": str(req.url)}})

    base.set_transport(httpx.MockTransport(handler))
    try:
        res = InstagramPublisher({"status_poll_seconds": 0}, {}, tmp_path).publish(_payload(tmp_path))
    finally:
        base.set_transport(None)
    assert res.remote_id == "M9" and res.url.endswith("/reel/abc/") and res.status == "posted"


def test_uploadpost_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "K")

    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == "Apikey K" and b'name="platform[]"' in req.content and b"youtube" in req.content
        return httpx.Response(200, json={"success": True, "results": {"youtube": {"success": True, "url": "https://youtu.be/q", "video_id": "q"}}})

    base.set_transport(httpx.MockTransport(handler))
    try:
        pub = UploadPostPublisher("youtube", {"privacy": "public"}, {"upload_post": {"user": "ryan"}}, tmp_path)
        assert pub.configured()[0]
        res = pub.publish(_payload(tmp_path))
    finally:
        base.set_transport(None)
    assert res.url == "https://youtu.be/q" and res.remote_id == "q"
