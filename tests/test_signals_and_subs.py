import numpy as np

from clipador.detect import signals as sig
from clipador.detect.transcribe import Transcript, Word
from clipador.edit.subtitles import build_ass, group_lines


def test_chat_signal_peaks_on_burst():
    base = 1000.0
    msgs = [{"ts": base + 5 + i * 0.5, "text": "oi"} for i in range(10)]  # ritmo normal 2 msg/s
    msgs += [{"ts": base + 40 + i * 0.05, "text": "KKKKKKKK"} for i in range(60)]  # explosão
    s, info = sig.chat_signal(msgs, base, 60, baseline_rate=2.0, baseline_std=1.0, hype_tokens=["kkk"])
    assert s[40:43].max() > 0.8
    assert s[20] < 0.3
    assert info["messages"] == 70


def test_keyword_signal_finds_compound_keywords():
    words = [Word(0, 0.3, "olha"), Word(0.4, 0.7, "a"), Word(0.8, 1.2, "polícia"), Word(5, 5.3, "vamos"),
             Word(10, 10.4, "easter"), Word(10.5, 10.9, "egg")]
    tr = Transcript(words=words, text="olha a polícia vamos easter egg")
    s, found = sig.keyword_signal(tr, ["polícia", "easter egg", "banco"], 15)
    assert "polícia" in found and "easter egg" in found and "banco" not in found
    assert s[0] == 1.0 and s[10] == 1.0 and s[6] == 0.0


def test_blocklist_hits_normalizes_accents():
    tr = Transcript(words=[], text="isso foi um ESTUPRO de regra")
    assert sig.blocklist_hits(tr, [], ["estupr"]) == ["estupr"]
    assert sig.blocklist_hits(Transcript(words=[], text="tudo bem"), [{"text": "ok"}], ["estupr"]) == []


def test_group_lines_breaks_on_pause_and_size():
    words = [Word(0, 0.3, "a"), Word(0.4, 0.7, "b"), Word(0.8, 1.1, "c"), Word(1.2, 1.5, "d"),
             Word(3.0, 3.3, "e")]
    lines = group_lines(words, max_words=3, max_seconds=1.8)
    assert [len(l) for l in lines] == [3, 1, 1]


def test_build_ass_highlights_current_word():
    words = [Word(0, 0.3, "mano"), Word(0.4, 0.8, "olha"), Word(0.9, 1.2, "isso")]
    ass = build_ass(words, {"font_name": "Arial", "font_size": 70, "highlight_color": "&H0000FFFF"}, margin_v=500)
    assert "PlayResX: 1080" in ass and "MarginV" in ass.split("[V4+ Styles]")[1]
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 3
    assert events[0].endswith("{\\c&H0000FFFF\\fscx125\\fscy125\\t(0,100,\\fscx112\\fscy112)}MANO{\\r} OLHA ISSO")
    assert "KKKK" not in ass


def test_build_ass_no_overlap_between_lines_and_merges_hyphen_fragments():
    from clipador.edit.subtitles import merge_fragments

    words = [Word(0, 0.3, "um"), Word(0.35, 0.6, "dois"), Word(0.65, 0.9, "três"), Word(1.0, 1.3, "post"),
             Word(1.3, 1.5, "-it"), Word(1.6, 1.9, "fim")]
    merged = merge_fragments(words)
    assert [w.text for w in merged] == ["um", "dois", "três", "post-it", "fim"]
    ass = build_ass(words, {"uppercase": True}, margin_v=500)
    events = [l.split(",", 3)[1:3] for l in ass.splitlines() if l.startswith("Dialogue:")]

    def secs(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    spans = [(secs(a), secs(b)) for a, b in events]
    assert all(spans[i][1] <= spans[i + 1][0] + 1e-6 for i in range(len(spans) - 1))  # nada se sobrepõe
    assert "-IT" not in ass.replace("POST-IT", "")


def test_parse_youtube_live_chat_line_skips_history():
    import json as _j

    from clipador.capture.chat import parse_yt_chat_line

    def item(ts, text, author="fã"):
        return {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
            "timestampUsec": str(ts), "authorName": {"simpleText": author},
            "message": {"runs": [{"text": text}, {"emoji": {"shortcuts": [":fire:"]}}]}}}}}

    line = _j.dumps({"replayChatItemAction": {"actions": [item(1_000, "velha"), item(5_000_000, "KKKK"),
                                                         {"addBannerToLiveChatCommand": {}}]}})
    assert parse_yt_chat_line(line, min_usec=2_000_000) == [("fã", "KKKK:fire:")]
    assert parse_yt_chat_line("{quebrado") == []
