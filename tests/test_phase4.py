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
