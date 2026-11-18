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
