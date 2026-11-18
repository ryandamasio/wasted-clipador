import json
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from clipador.config import Streamer, load_config
from clipador.edit.render import Renderer, pick_zoom_peaks, read_music_licenses, zoom_expression
from clipador.review.app import create_app
from clipador.review.service import ReviewError, ReviewService
from clipador.store.db import Database


# ------------------------------------------------------------------ render helpers
def test_zoom_peaks_include_detected_peak_and_loud_bursts():
    sr = 16000
    audio = np.random.default_rng(0).normal(0, 0.01, sr * 20).astype(np.float32)
    audio[sr * 12: sr * 12 + sr // 2] *= 40  # grito em 12 s
    peaks = pick_zoom_peaks(audio, 20.0, must_include=5.0, max_peaks=3, min_gap=4.0)
    assert 5.0 in peaks
    assert any(abs(p - 12.2) < 0.6 for p in peaks)
    assert all(1.0 <= p <= 19.0 for p in peaks)
    assert peaks == sorted(peaks)


def test_zoom_expression_shape():
    assert zoom_expression([], 30) == "1"
    z = zoom_expression([5.0, 12.25], 30, amount=0.12, width=1.2)
    assert z.startswith("1+0.120*(") and z.count("cos(") == 2 and "in/30" in z
    assert "," in z  # vírgulas dentro da expressão são protegidas por aspas no filtro


def test_layout_center_and_split():
    cfg = load_config()
    r = Renderer.__new__(Renderer)
    r.cfg, r.e = cfg, cfg.settings.section("edit")
    plain = Streamer(name="X", handle="x", platform="twitch", url="u")
    lay = r._layout(plain, (1920, 1080))
    assert lay["mode"] == "center" and lay["game"] == (1080, 868, (1920 - 868) // 2)
    cam = Streamer(name="Y", handle="y", platform="twitch", url="u", facecam={"x": 0.75, "y": 0.0, "w": 0.25, "h": 0.30})
    lay = r._layout(cam, (1920, 1080))
    face_w, face_h, face_y, crop = lay["face"]
    game_w, game_h, game_y = lay["game"]
    assert lay["mode"] == "split" and face_w == 1080 and face_h % 2 == 0 and game_h == 868
    assert game_y == face_y + face_h  # gameplay logo abaixo da facecam
    assert crop[2] == 1440 and crop[0] == 480 and crop[3] == 0  # recorte do canto superior direito
    assert face_y >= 0 and game_y + game_h <= 1920
    # facecam muito alta (recorte quase quadrado) é limitada em altura e perde largura
    tall = Streamer(name="Z", handle="z", platform="twitch", url="u", facecam={"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.5})
    face_w2, face_h2, *_ = r._layout(tall, (1920, 1080))["face"]
    assert face_h2 == 760 and face_w2 < 1080
    graph, _ = r._filter_graph(Path("subs.ass"), cam, (1920, 1080), [3.0])
    assert "split=3" in graph and "zoompan=" in graph and "crop=480:" in graph and "crop=1344:ih:96:0" in graph and "ass=subs.ass" in graph


def test_subtitle_margin_avoids_game_subtitles_and_credit():
    import random as _r

    cfg = load_config()
    r = Renderer.__new__(Renderer)
    r.cfg, r.e = cfg, cfg.settings.section("edit")
    plain = Streamer(name="X", handle="x", platform="twitch", url="u")
    cam = Streamer(name="Y", handle="y", platform="twitch", url="u", facecam={"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.27})
    for s in (plain, cam):
        lay = r._layout(s, (1920, 1080))
        _, gh, gy = lay["game"]
        for seed in range(20):
            m = r._subtitle_margin(s, (1920, 1080), _r.Random(seed))
            text_bottom = 1920 - m
            assert text_bottom <= 1920 - 190  # acima do crédito
            if lay["mode"] == "center":
                assert text_bottom - 98 >= gy + gh - 20  # texto abaixo do gameplay, não sobre a legenda do jogo
            else:
                assert gy + gh * 0.6 <= text_bottom <= gy + gh * 0.8


def test_read_music_licenses(tmp_path):
    (tmp_path / "LICENSES.md").write_text(
        "| Arquivo | Título | Autor | Fonte | Licença | Atribuição |\n|---|---|---|---|---|---|\n"
        "| `a.mp3` | Faixa A | Fulano | https://x | CC BY 4.0 | Faixa A by Fulano (CC BY) |\n"
        "| b.mp3 | Faixa B | Beltrano | https://y | CC0 | |\n", encoding="utf-8")
    lic = read_music_licenses(tmp_path)
    assert lic["a.mp3"]["license"] == "CC BY 4.0" and lic["a.mp3"]["attribution"].startswith("Faixa A")
    assert lic["b.mp3"]["attribution"] == ""


# ------------------------------------------------------------------ review service / API
def _seed(tmp_path: Path):
    cfg = load_config()
    db = Database(tmp_path / "t.sqlite")
    sid = db.create_stream("coringa", "kick", "https://kick.com/coringa", tmp_path)
    seg_path = tmp_path / "seg0.ts"
    seg_path.write_bytes(b"0" * 1000)
    seg = db.add_segment(sid, 0, seg_path, 100.0, 220.0)
    mid = db.add_moment(sid, seg, 150.0, 175.0, 152.0, 80.0,
                        {"notes": ["chat explodiu"], "chat": 0.9, "audio": 0.4, "keywords_found": ["polícia"]}, "fala", [])
    db.update("moments", mid, status="clipped")
    clip_dir = tmp_path / "out" / "coringa_x_m1"
    clip_dir.mkdir(parents=True)
    (clip_dir / "clip.mp4").write_bytes(b"\x00" * 20000)
    (clip_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    copy_data = {"titles": ["T1", "T2", "T3"], "networks": {"youtube": {"title": "T1 #shorts", "description": "d1", "hashtags": ["#gta6"]},
                                                           "tiktok": {"title": "T2", "description": "d2", "hashtags": ["#gta6"]}},
                 "credit": "c", "music": None, "music_note": "n"}
    cid = db.add_clip(mid, clip_dir / "clip.mp4", clip_dir / "cover.jpg", None, 25.0, "T1", copy_data,
                      render_info={"layout": "center", "zoom": [2.0, 9.5]})
    return cfg, db, sid, seg, mid, cid, clip_dir


def test_review_service_flow(tmp_path):
    cfg, db, sid, seg, mid, cid, clip_dir = _seed(tmp_path)
    svc = ReviewService(cfg, db)
    items = svc.list_clips("ready")
    assert len(items) == 1 and items[0]["score"] == 80 and items[0]["media_available"] and items[0]["render"]["zoom"] == [2.0, 9.5]

    out = svc.approve(cid, title="Título editado", networks={"tiktok": {"description": "nova desc"}})
    assert out["status"] == "approved" and out["chosen_title"] == "Título editado"
    assert out["networks"]["tiktok"]["description"] == "nova desc" and out["titles"][0] == "Título editado"
    assert (clip_dir / "post.md").exists() and "Título editado" in (clip_dir / "post.md").read_text(encoding="utf-8")

    svc.restore(cid)
    assert db.get_clip(cid)["status"] == "ready"
    svc.reject(cid, "sem graça")
    assert db.get_clip(cid)["status"] == "rejected" and db.get_moment(mid)["status"] == "rejected"
    svc.restore(cid)

    # recorte: começa 2 s antes, termina 3 s depois -> momento volta para a fila, clipe antigo substituído
    svc.recut(cid, -2.0, 3.0)
    m = db.get_moment(mid)
    assert (m["start_ts"], m["end_ts"], m["status"], m["recut_count"]) == (148.0, 178.0, "new", 1)
    assert db.get_clip(cid)["status"] == "superseded"
    assert db.pending_moments()[0]["id"] == mid

    stats = db.review_stats()
    assert sum(r["total"] for r in stats) == 5  # approved, restored, rejected, restored, recut
    assert stats[0]["band"] == "70-84"

    # recorte fora dos limites
    db.update("moments", mid, status="clipped")
    db.set_clip_status(cid, "ready")
    try:
        svc.recut(cid, -40.0, 40.0)
        assert False, "deveria falhar"
    except ReviewError:
        pass
    # mídia apagada
    (tmp_path / "seg0.ts").unlink()
    try:
        svc.recut(cid, -1.0, 0.0)
        assert False, "deveria falhar"
    except ReviewError as exc:
        assert "apagada" in str(exc)


def test_purge_old_rejected(tmp_path, monkeypatch):
    cfg, db, sid, seg, mid, cid, clip_dir = _seed(tmp_path)
    monkeypatch.setitem(cfg.settings._data["paths"], "output_dir", str(tmp_path / "out"))
    svc = ReviewService(cfg, db)
    svc.reject(cid, "x")
    assert svc.purge_old(1) == 0  # recém-rejeitado
    db.update("clips", cid, reviewed_at=0.0)
    assert svc.purge_old(1) == 1
    assert not clip_dir.exists() and db.get_clip(cid)["status"] == "purged"


def test_review_api(tmp_path):
    cfg, db, sid, seg, mid, cid, clip_dir = _seed(tmp_path)
    client = TestClient(create_app(cfg, db))
    assert client.get("/").status_code == 200 and "fila de aprovação" in client.get("/").text
    j = client.get("/api/clips?status=ready").json()
    assert j["clips"][0]["id"] == cid and j["stats"]["counts"]["ready"] == 1
    assert client.get(f"/media/{cid}/clip.mp4").status_code == 200
    assert client.get(f"/media/{cid}/clip_youtube.mp4").status_code == 404
    assert client.get(f"/media/{cid}/../x").status_code in (404, 400)
    r = client.post(f"/api/clips/{cid}/approve", json={"title": "T2"})
    assert r.status_code == 200 and r.json()["chosen_title"] == "T2"
    assert client.post(f"/api/clips/{cid}/recut", json={"start_delta": -100}).status_code == 400
    # a aprovação agendou um post por rede habilitada (4, todas manuais na config padrão)
    assert db.fetchone("SELECT COUNT(*) AS n FROM posts WHERE status='scheduled'")["n"] == 4
    r = client.post(f"/api/clips/{cid}/posted", json={"platforms": ["youtube", "tiktok"]})
    assert r.json()["status"] == "posted"
    assert db.fetchone("SELECT COUNT(*) AS n FROM posts WHERE status='posted'")["n"] == 2
    assert client.get("/api/clips?status=posted").json()["clips"][0]["id"] == cid
