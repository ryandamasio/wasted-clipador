from clipador.detect.content import adjust, analyze


def test_sub_thanks_are_penalized():
    t = ("Piriquito, valeu pelos 23 meses! Opina, obrigado pelos 7 meses de sub, Milena 10 meses, "
         "um beijo, chat, eu estou com sono, tamo junto")
    info = analyze(t)
    assert info["chat_talk"] >= 4 and adjust(70, info) == 35


def test_gta6_first_impressions_get_bonus():
    t = "Mano, olha o mar! Que gráfico é esse, meu deus, não acredito, é muito realista"
    info = analyze(t)
    assert len(info["viral"]) >= 4 and adjust(60, info) == 80   # +5 por reação, teto +20


def test_neutral_talk_unchanged():
    assert adjust(55, analyze("vou ali pegar o carro e depois a gente vê")) == 55


def test_cutscene_seconds_marks_english_windows():
    import numpy as np

    from clipador.detect.pipeline import Detector

    class FakeModel:
        def __init__(self):
            self.calls = 0

        def detect_language(self, audio):
            self.calls += 1
            en = 0.8 if self.calls == 2 else 0.05   # segunda janela de 30 s = diálogo do jogo
            return ("en" if en > .5 else "pt"), en, [("en", en), ("pt", 1 - en)]

    class FakeTranscriber:
        import threading
        lock = threading.Lock()
        model = FakeModel()

    d = Detector.__new__(Detector)
    d.d = {"cutscene_english_prob": 0.5}
    d.transcriber = FakeTranscriber()
    out = d.cutscene_seconds(np.zeros(16000 * 90, dtype=np.float32), 90)
    assert out[:30].sum() == 0 and out[30:60].all() and out[60:].sum() == 0
