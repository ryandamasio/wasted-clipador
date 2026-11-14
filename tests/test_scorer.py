import numpy as np

from clipador.detect.scorer import combine, overlap_ratio, pick_peaks, refine_bounds
from clipador.detect.transcribe import Transcript, Word


def test_combine_renormalizes_missing_signals():
    audio = np.zeros(60, dtype=np.float32)
    audio[30] = 1.0
    score, eff = combine({"chat": None, "audio": audio, "transcript": np.zeros(60), "keywords": np.zeros(60)},
                         {"chat": 0.45, "audio": 0.25, "transcript": 0.15, "keywords": 0.15}, 8)
    assert "chat" not in eff
    assert abs(sum(eff.values()) - 1.0) < 1e-6
    assert score.argmax() in range(26, 35)
    assert score.max() > 20


def test_pick_peaks_respects_gap_and_threshold():
    score = np.zeros(200)
    score[50] = 90
    score[60] = 85   # muito perto do 50
    score[150] = 70
    score[180] = 40  # abaixo do limiar
    peaks = pick_peaks(score, min_score=55, min_gap=45, max_peaks=3)
    assert peaks == [50, 150]


def _transcript():
    words = []
    t = 0.0
    for i in range(80):
        words.append(Word(t, t + 0.3, f"w{i}"))
        t += 0.4
        if i % 10 == 9:
            t += 1.0  # pausa de frase
    return Transcript(words=words, text=" ".join(w.text for w in words))


def test_refine_bounds_starts_at_sentence_and_ends_at_pause():
    tr = _transcript()
    cfg = {"pre_roll_seconds": 1.5, "min_seconds": 15, "max_seconds": 40, "target_seconds": 25}
    start, end = refine_bounds(20.0, tr, cfg)
    assert 15 <= end - start <= 40
    # início deve coincidir com começo de frase (palavra após pausa), menos 0.25
    sentence_starts = [round(tr.words[i].start - 0.25, 2) for i in range(len(tr.words))
                       if i == 0 or tr.words[i].start - tr.words[i - 1].end >= 0.5]
    assert start in sentence_starts
    assert start <= 20.0  # nunca depois do pico
    # fim deve estar logo após o fim de uma palavra seguida de pausa
    pause_ends = [round(tr.words[i].end + 0.35, 2) for i in range(len(tr.words))
                  if i == len(tr.words) - 1 or tr.words[i + 1].start - tr.words[i].end >= 0.45]
    assert end in pause_ends


def test_refine_bounds_without_transcript_uses_target():
    start, end = refine_bounds(30.0, None, {"pre_roll_seconds": 1.5, "min_seconds": 15, "max_seconds": 40, "target_seconds": 25})
    assert start == 28.5 and end == 53.5


def test_overlap_ratio():
    assert overlap_ratio(0, 10, 5, 15) == 0.5
    assert overlap_ratio(0, 10, 20, 30) == 0.0
    assert overlap_ratio(0, 10, 2, 8) == 1.0
