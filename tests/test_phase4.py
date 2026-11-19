import json
from pathlib import Path

from fastapi.testclient import TestClient

from clipador.capture.recorder import Recorder
from clipador.config import Streamer, load_config
from clipador.review.app import create_app
from clipador.store.db import Database


def test_db_backup_and_rotation(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    db.create_stream("coringa", "kick", "u", tmp_path)
    dest = tmp_path / "backups"
    outs = [db.backup(dest, keep=2) for _ in range(3)]
    assert all(o.name.startswith("clipador_") for o in outs)
    assert len(list(dest.glob("clipador_*.sqlite"))) == 2 and db.last_backup(dest) is not None
    copy = Database(db.last_backup(dest))
    assert copy.stats()["streams"] == 1


def test_blocklist_hides_streamer(tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(cfg, "blocklist_path", staticmethod(lambda: tmp_path / "blocklist.yaml"))
    assert "coringa" in {s.handle for s in cfg.enabled_streamers}
    cfg.block_streamer("Coringa", "pediu remoção")
    assert cfg.blocklist() == {"coringa"}
    assert "coringa" not in {s.handle for s in cfg.enabled_streamers}
    text = (tmp_path / "blocklist.yaml").read_text(encoding="utf-8")
    assert "pediu remoção" in text and text.startswith("#")


def test_remove_streamer_data_and_metrics_api(tmp_path):
    cfg = load_config()
    db = Database(tmp_path / "t.sqlite")
    sid = db.create_stream("coringa", "kick", "https://kick.com/coringa", tmp_path)
    seg = db.add_segment(sid, 0, tmp_path / "s.ts", 100.0, 220.0)
    mid = db.add_moment(sid, seg, 150.0, 175.0, 152.0, 80.0, {"notes": []}, "t", [])
    db.update("moments", mid, status="clipped")
    d = tmp_path / "out" / "c"; d.mkdir(parents=True); (d / "clip.mp4").write_bytes(b"0" * 100)
    cid = db.add_clip(mid, d / "clip.mp4", None, None, 25.0, "T", {"titles": ["T"], "networks": {}})
    db.add_post(cid, "youtube", 1e12, "T", "c", "manual")
    client = TestClient(create_app(cfg, db))
    m = client.get("/api/metrics?days=7").json()
    assert m["counts"]["ready"] == 1 and m["per_streamer"][0]["streamer"] == "coringa" and "disk_free_gb" in m["health"]
    res = db.remove_streamer_data("coringa")
    assert res["clips"] == 1 and res["dirs"] == [str(d)]
    assert db.get_clip(cid)["status"] == "rejected" and db.get_moment(mid)["status"] == "rejected"
    assert db.posts_for_clip(cid)[0]["status"] == "skipped"


def test_source_commands_kick_headless_flag_and_file_live(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    kick = Streamer(name="C", handle="c", platform="kick", url="https://kick.com/c")
    rec = Recorder(db, kick, {"segment_seconds": 120}, tmp_path / "rec", browser="C:/chrome.exe")
    cmd = rec._source_cmd(False)
    # streamlink 8 exige valor explícito; sem "=true" ele engolia a URL como valor da flag
    assert "--webbrowser-headless=true" in cmd and cmd[-2:] == ["https://kick.com/c", "best"]
    fake = Streamer(name="F", handle="f", platform="twitch", url="file:///D:/x/src.ts")
    rec2 = Recorder(db, fake, {"segment_seconds": 120}, tmp_path / "rec", browser=None)
    cmd2 = rec2._source_cmd(False)
    assert cmd2[0] == "ffmpeg" and "-re" in cmd2 and "D:/x/src.ts" in cmd2 and cmd2[-1] == "pipe:1"
    from clipador.capture.monitor import check_online
    assert check_online("file:///" + str(tmp_path / "nope.ts").replace("\\", "/"), "twitch", None) == (False, "arquivo (teste)")
    db.close()


def test_short_moment_is_extended_to_one_minute(tmp_path):
    from clipador.edit.render import MediaNotReady
    from clipador.orchestrator import Orchestrator

    cfg = load_config()
    db = Database(tmp_path / "t.sqlite")
    sid = db.create_stream("coringa", "kick", "https://kick.com/coringa", tmp_path)
    seg = db.add_segment(sid, 0, tmp_path / "s.ts", 100.0, 220.0)
    mid = db.add_moment(sid, seg, 150.0, 175.0, 152.0, 80.0, {"notes": []}, "t", [])
    orch = Orchestrator(cfg, db)
    seen = {}

    class FakeRenderer:
        def render(self, m, segments, streamer, out_dir, live, long_segments=None, **kw):
            seen["dur"] = m["end_ts"] - m["start_ts"]
            raise MediaNotReady("teste")

    orch.renderer = FakeRenderer()
    orch.process_moment(db.get_moment(mid))
    min_s = float(cfg.settings.get("detect.clip.min_seconds"))
    assert min_s >= 60 and seen["dur"] == min_s
    assert db.get_moment(mid)["start_ts"] == 175.0 - min_s
    db.close()


def test_moment_past_end_of_ended_recording_slides_back(tmp_path):
    from clipador.edit.render import MediaNotReady
    from clipador.orchestrator import Orchestrator

    cfg = load_config()
    db = Database(tmp_path / "t.sqlite")
    sid = db.create_stream("coringa", "kick", "https://kick.com/coringa", tmp_path)
    db.end_stream(sid, "ended")
    for i in range(2):
        p = tmp_path / f"s{i}.ts"; p.write_bytes(b"0")
        db.add_segment(sid, i, p, 100.0 + 120 * i, 220.0 + 120 * i)       # mídia até 340
    mid = db.add_moment(sid, None, 300.0, 372.0, 305.0, 80.0, {"notes": []}, "t", [])   # termina depois da mídia
    orch = Orchestrator(cfg, db)
    seen = {}

    class FakeRenderer:
        def render(self, m, segments, streamer, out_dir, live, long_segments=None, **kw):
            seen.update(start=m["start_ts"], end=m["end_ts"])
            raise MediaNotReady("teste")

    orch.renderer = FakeRenderer()
    orch.process_moment(db.get_moment(mid))
    assert seen["end"] == 340.0 and seen["end"] - seen["start"] >= 62
    short = db.add_moment(sid, None, 90.0, 400.0, 95.0, 80.0, {"notes": []}, "t", [])
    sid2 = db.create_stream("coringa", "kick", "u", tmp_path); db.end_stream(sid2, "ended")
    p = tmp_path / "x.ts"; p.write_bytes(b"0")
    db.add_segment(sid2, 0, p, 1000.0, 1030.0)                             # só 30 s de mídia
    m2 = db.add_moment(sid2, None, 1000.0, 1072.0, 1005.0, 80.0, {"notes": []}, "t", [])
    orch.process_moment(db.get_moment(m2))
    assert db.get_moment(m2)["status"] == "failed"
    db.close()


def test_gta_and_rerun_titles():
    from clipador.orchestrator import is_gta, is_rerun

    assert is_gta("GTA 6. (jogando no PS5)") and is_gta("VICE CITY BABYYYY") and is_gta("+18 SPEEDRUN DE GTA VI")
    assert not is_gta("gta rp cidade alta") and not is_gta("GTA V online")
    assert not is_gta("367 | noitinha :)") and not is_gta(None) and not is_gta("agtap")
    assert is_rerun("RERUN: Lobby do Alastro!") and not is_rerun("GTA 6 ao vivo")


def test_screen_capture_commands(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    s = Streamer(name="X", handle="x", platform="kick", url="https://kick.com/x")
    cap = {"segment_seconds": 120, "plan_b": {"enabled": True, "region": {"x": 1920, "y": 0, "w": 1280, "h": 720},
                                            "audio_device": "virtual-audio-capturer", "fps": 30}}
    rec = Recorder(db, s, cap, tmp_path / "rec", browser="C:/chrome.exe")
    cmd = rec._screen_ffmpeg_cmd(tmp_path / "run")
    joined = " ".join(cmd)
    assert "-f gdigrab" in joined and "-offset_x 1920" in joined and "-video_size 1280x720" in joined
    assert "audio=virtual-audio-capturer" in joined and "h264_nvenc" in joined and "-segment_time 120" in joined
    b = rec._browser_cmd()
    assert b[0] == "C:/chrome.exe" and "--kiosk" in b and "--window-position=1920,0" in b and b[-1] == s.url
    cap["plan_b"]["audio_device"] = ""
    assert "dshow" not in " ".join(rec._screen_ffmpeg_cmd(tmp_path / "run"))
    db.close()


def test_tunnel_watchdog_kills_dead_tunnel(tmp_path, monkeypatch):
    import httpx
    from clipador.review import tunnel as tmod

    t = tmod.Tunnel(8787, tmp_path / "u.txt")
    t.url = "https://x.trycloudflare.com"
    killed = []
    monkeypatch.setattr(t, "_kill", lambda: (killed.append(1), t.stop_event.set()))
    def dead(*a, **k):
        raise httpx.ConnectError("gone")
    monkeypatch.setattr(tmod.httpx, "get", dead)
    t._watchdog(every=0.01, max_fails=3)
    assert killed == [1]
