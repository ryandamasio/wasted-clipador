"""Legendas dinâmicas (ASS) palavra por palavra com destaque na palavra atual."""
from __future__ import annotations

import re

from clipador.detect.transcribe import Word

# correções leves de gírias/erros comuns do Whisper em pt-BR
SLANG_FIXES = {
    "kk": "KKKK", "kkk": "KKKK", "kkkk": "KKKK", "véio": "véi", "veio": "véi", "mano,": "mano", "pô": "pô",
    "ta": "tá", "to": "tô", "ne": "né", "pra": "pra", "vc": "você", "tbm": "também", "pqp": "PQP",
}


def _fix(word: str) -> str:
    bare = re.sub(r"[^\wÀ-ÿ]", "", word.lower())
    if bare in SLANG_FIXES:
        return SLANG_FIXES[bare]
    return word


def _ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def merge_fragments(words: list[Word]) -> list[Word]:
    """Junta pedaços que o Whisper separa com hífen ("post" + "-it") numa palavra só."""
    out: list[Word] = []
    for w in words:
        t = w.text.strip()
        if out and t.startswith("-") and len(t) > 1 and w.start - out[-1].end < 0.3:
            p = out[-1]
            out[-1] = Word(p.start, w.end, p.text.rstrip() + t, min(p.prob, w.prob))
        else:
            out.append(w)
    return out


def group_lines(words: list[Word], max_words: int = 3, max_seconds: float = 1.8) -> list[list[Word]]:
    lines: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.end - cur[0].start > max_seconds or w.start - cur[-1].end > 0.7
                    or re.search(r"[.!?]$", cur[-1].text)):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def build_ass(words: list[Word], style: dict, play_w: int = 1080, play_h: int = 1920, margin_v: int = 560) -> str:
    font = style.get("font_name", "Arial")
    size = int(style.get("font_size", 78))
    bold = -1 if style.get("bold", True) else 0
    primary = style.get("primary_color", "&H00FFFFFF")
    highlight = style.get("highlight_color", "&H0000FFFF")
    outline_c = style.get("outline_color", "&H00000000")
    outline = int(style.get("outline", 5))
    shadow = int(style.get("shadow", 1))
    upper = bool(style.get("uppercase", True))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font},{size},{primary},{primary},{outline_c},&H80000000,{bold},0,0,0,100,100,1,0,1,{outline},{shadow},2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    lines = group_lines(merge_fragments(words), int(style.get("max_words_per_line", 3)), float(style.get("max_line_seconds", 1.8)))
    for li, line in enumerate(lines):
        texts = [_fix(w.text) for w in line]
        if upper:
            texts = [t.upper() for t in texts]
        next_start = lines[li + 1][0].start if li + 1 < len(lines) else None
        for i, w in enumerate(line):
            start = w.start
            end = line[i + 1].start if i + 1 < len(line) else w.end + 0.25
            if next_start is not None:
                end = min(end, next_start)  # nunca duas linhas na tela ao mesmo tempo
            if end - start < 0.08:
                end = start + 0.08
            parts = []
            for j, t in enumerate(texts):
                t = t.replace("{", "").replace("}", "")
                if j == i:
                    # "pop": a palavra entra a 125% e assenta em 112% em 0,1 s
                    parts.append(f"{{\\c{highlight}\\fscx125\\fscy125\\t(0,100,\\fscx112\\fscy112)}}{t}{{\\r}}")
                else:
                    parts.append(t)
            text = " ".join(parts)
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Word,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"
