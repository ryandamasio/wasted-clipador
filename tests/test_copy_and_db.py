import json

from clipador.config import load_config
from clipador.copy.generator import NETWORKS, generate_copy
from clipador.store.db import Database


def _moment(mid=7, transcript="Olha a polícia atrás de mim mano! Não acredito nisso.", **reasons):
    base = {"chat": 0.8, "audio": 0.6, "transcript": 0.4, "keywords": 1.0, "keywords_found": ["polícia"],
            "notes": ["chat explodiu"]}
    base.update(reasons)
    return {"id": mid, "score": 77.0, "reasons": json.dumps(base), "transcript": transcript,
            "start_ts": 1000.0, "end_ts": 1025.0, "peak_ts": 1002.0}


def test_generate_copy_has_three_titles_and_all_networks():
    cfg = load_config()
    streamer = cfg.streamer("coringa")
    data = generate_copy(cfg, streamer, _moment())
    assert len(data["titles"]) == 3 and len(set(data["titles"])) == 3
    assert set(data["networks"]) == set(NETWORKS)
    limits = cfg.hashtags["limits"]
    for net, d in data["networks"].items():
        assert len(d["hashtags"]) <= limits[net]
        assert d["hashtags"][0] == "#gta6"
        assert streamer.channel_link in d["description"]
    assert data["networks"]["youtube"]["title"].endswith("#shorts")
    assert "Clipe de Coringa" in data["credit"]


def test_generate_copy_is_deterministic_per_moment():
    cfg = load_config()
    s = cfg.streamer("gaules")
    a = generate_copy(cfg, s, _moment(3))
    b = generate_copy(cfg, s, _moment(3))
    assert a == b


def test_db_roundtrip(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    sid = db.create_stream("coringa", "kick", "https://kick.com/coringa", tmp_path)
    seg = db.add_segment(sid, 0, tmp_path / "seg0.ts", 100.0, 220.0)
    db.add_chat_messages([(sid, 150.0, "u", "kkk"), (sid, 151.0, "v", "pog")])
    assert db.chat_count(sid) == 2
    assert db.next_segment_to_detect()["id"] == seg
    mid = db.add_moment(sid, seg, 150.0, 175.0, 152.0, 80.0, {"notes": []}, "txt", [])
    assert db.pending_moments()[0]["id"] == mid
    assert db.moments_overlapping(sid, 160.0, 190.0)[0]["id"] == mid
    db.update("segments", seg, status="detected")
    # segmento antigo com momento pendente NÃO pode ser apagado
    assert db.old_processed_segments(9e12) == []
    db.update("moments", mid, status="clipped")
    assert len(db.old_processed_segments(9e12)) == 1
    assert db.segments_covering(sid, 149.0, 176.0)[0]["id"] == seg
