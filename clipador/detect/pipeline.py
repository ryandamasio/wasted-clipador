"""Detecção de momentos para um segmento gravado: chat + áudio + Whisper + palavras-chave (+ LLM opcional)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from clipador.config import Config
from clipador.detect import signals as sig
from clipador.detect.audio import decode_audio, per_second_energy_signal
from clipador.detect.content import adjust, analyze
from clipador.detect.llm import LLMScorer
from clipador.detect.scorer import combine, overlap_ratio, pick_peaks, refine_bounds
from clipador.detect.transcribe import Transcriber, Transcript
from clipador.store.db import Database

log = logging.getLogger("detect.pipeline")


def chat_only(comps: dict) -> bool:
    """Chat explodiu mas o streamer não reagiu (áudio < 0.4 e fala < 0.3)."""
    return (comps.get("chat") or 0) >= 0.5 and comps.get("audio", 0) < 0.4 and comps.get("transcript", 0) < 0.3


class Detector:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.d = cfg.settings.section("detect")
        self.transcriber = Transcriber.get(self.d.get("whisper", {}))
        self.llm = LLMScorer(self.d.get("llm", {}))
        self._audio_baseline: dict[int, tuple[float, float]] = {}

    # ------------------------------------------------------------------
    def chat_offset(self, streamer_handle: str) -> float:
        s = self.cfg.streamer(streamer_handle)
        if s and s.chat_offset_seconds is not None:
            return float(s.chat_offset_seconds)
        return float(self.cfg.settings.get("capture.chat_offset_seconds", 10))

    def _chat_baseline(self, stream_id: int, before_chat_ts: float) -> tuple[float, float]:
        minutes = int(self.d.get("chat", {}).get("baseline_minutes", 10))
        msgs = self.db.chat_between(stream_id, before_chat_ts - minutes * 60, before_chat_ts)
        if len(msgs) < 10:
            return 0.0, 0.0
        bins = np.zeros(minutes * 60, dtype=np.float32)
        for m in msgs:
            i = int(m["ts"] - (before_chat_ts - minutes * 60))
            if 0 <= i < len(bins):
                bins[i] += 1
        smooth = np.convolve(bins, np.ones(5) / 5, mode="same")
        return float(smooth.mean()), float(smooth.std())

    def transcribe_segment(self, segment: dict, audio: np.ndarray) -> Transcript:
        tpath = Path(segment["path"]).with_suffix(".transcript.json")
        if tpath.exists():
            try:
                return Transcript.from_json(json.loads(tpath.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
        t0 = time.time()
        transcript = self.transcriber.transcribe(audio)
        tpath.write_text(json.dumps(transcript.to_json(), ensure_ascii=False), encoding="utf-8")
        self.db.update("segments", segment["id"], transcript_path=str(tpath))
        log.info("segmento %d transcrito em %.1fs (%d palavras)", segment["id"], time.time() - t0, len(transcript.words))
        return transcript

    # ------------------------------------------------------------------
    def detect_segment(self, segment: dict) -> list[int]:
        """Processa um segmento e grava momentos. Retorna ids criados."""
        seg_id = segment["id"]
        stream = self.db.get_stream(segment["stream_id"])
        if not stream:
            raise RuntimeError(f"stream {segment['stream_id']} não encontrada")
        self.db.update("segments", seg_id, status="detecting")
        path = Path(segment["path"])
        total = int(round(segment["duration"]))
        if total < 5 or not path.exists():
            self.db.update("segments", seg_id, status="failed")
            return []

        # --- áudio
        audio = decode_audio(path)
        if len(audio) == 0:
            log.warning("segmento %d sem áudio: nada a detectar", seg_id)
            self.db.update("segments", seg_id, status="detected")
            return []
        total = max(total, int(len(audio) / 16000))
        baseline = self._audio_baseline.get(stream["id"])
        audio_sig, new_baseline = per_second_energy_signal(audio, total, float(self.d.get("audio", {}).get("rms_window_seconds", 0.5)), baseline)
        self._audio_baseline[stream["id"]] = new_baseline

        # --- chat
        offset = self.chat_offset(stream["streamer"])
        seg_chat_start = segment["start_ts"] - offset
        messages = self.db.chat_between(stream["id"], seg_chat_start, seg_chat_start + total)
        chat_sig = None
        chat_info = {"messages": 0}
        if messages:
            base_rate, base_std = self._chat_baseline(stream["id"], seg_chat_start)
            if base_rate <= 0:
                base_rate = len(messages) / max(total, 1)
                base_std = base_rate * 0.6
            chat_sig, chat_info = sig.chat_signal(messages, seg_chat_start, total, base_rate, base_std,
                                                  self.d.get("chat", {}).get("hype_tokens", []),
                                                  float(self.d.get("chat", {}).get("min_burst_rate", 2.0)))

        # --- transcrição
        transcript = self.transcribe_segment(segment, audio)
        trans_sig = sig.transcript_signal(transcript, total)
        kw_sig, keywords_found = sig.keyword_signal(transcript, self.d.get("keywords", []), total)

        # --- combinação
        score, eff_weights = combine(
            {"chat": chat_sig, "audio": audio_sig, "transcript": trans_sig, "keywords": kw_sig},
            self.d.get("weights", {}), int(self.d.get("window_seconds", 8)))
        if len(score) == 0:
            self.db.update("segments", seg_id, status="detected")
            return []

        # penaliza conteúdo problemático (todo o segmento, por segurança)
        blocked = sig.blocklist_hits(transcript, messages, self.d.get("blocklist_words", []))
        if blocked:
            score = score * 0.4
            log.warning("segmento %d com termos bloqueados %s: nota reduzida", seg_id, blocked)
        sensitive = sig.blocklist_hits(transcript, messages, self.d.get("sensitive_words", []))
        if sensitive:
            score = score * 0.7
            log.info("segmento %d com tema sensível %s: nota reduzida", seg_id, sensitive)

        # sem chat os pesos renormalizam e áudio/palavras-chave sozinhos chegam fácil à nota: exige mais
        min_score = float(self.d.get("min_score", 55))
        if chat_sig is None and self.d.get("min_score_no_chat") is not None:
            min_score = max(min_score, float(self.d["min_score_no_chat"]))
        peaks = pick_peaks(score, min_score, int(self.d.get("min_gap_seconds", 45)),
                           int(self.d.get("max_moments_per_segment", 3)))
        log.info("segmento %d: nota máx %.0f, %d pico(s), chat=%d msgs, pesos=%s", seg_id, float(score.max()),
                 len(peaks), chat_info.get("messages", 0), {k: round(v, 2) for k, v in eff_weights.items()})

        created: list[int] = []
        clip_cfg = self.d.get("clip", {})
        cap = int(self.d.get("max_moments_per_hour", 0) or 0)
        recent = self.db.moments_overlapping(stream["id"], segment["start_ts"] - 3600, segment["end_ts"]) if cap else []
        for p in peaks:
            if cap and len(recent) + len(created) >= cap and float(score[p]) < float(self.d.get("always_keep_score", 70)):
                log.info("pico em %.0fs (nota %.0f) descartado: teto de %d momentos/hora nesta live", p, float(score[p]), cap)
                continue
            start_rel, end_rel = refine_bounds(float(p), transcript, clip_cfg)
            start_ts = segment["start_ts"] + start_rel
            end_ts = segment["start_ts"] + end_rel
            # dedupe
            dupes = self.db.moments_overlapping(stream["id"], start_ts, end_ts)
            if any(overlap_ratio(start_ts, end_ts, m["start_ts"], m["end_ts"]) > float(clip_cfg.get("dedupe_overlap", 0.5)) for m in dupes):
                log.info("pico em %.0fs descartado (duplicado)", p)
                continue
            words_abs = [[round(segment["start_ts"] + w.start, 3), round(segment["start_ts"] + w.end, 3), w.text]
                         for w in transcript.words_between(start_rel, end_rel)]
            text = " ".join(w[2] for w in words_abs)
            comps = {
                "chat": round(float(chat_sig[p]), 3) if chat_sig is not None else None,
                "audio": round(float(audio_sig[p]), 3),
                "transcript": round(float(trans_sig[p]), 3),
                "keywords": round(float(kw_sig[p]), 3),
                "keywords_found": [k for k in keywords_found if k.lower() in text.lower()][:6],
                "chat_info": chat_info,
                "weights": {k: round(v, 2) for k, v in eff_weights.items()},
                "blocked": blocked,
                "sensitive": sensitive,
            }
            final_score = float(score[p])
            content = analyze(text)
            comps["content"] = content
            final_score = adjust(final_score, content, float(self.d.get("chat_talk_penalty", 0.5)))
            if content["chat_talk"] >= 2 and content["chat_talk"] > len(content["viral"]):
                if final_score < min_score:
                    log.info("pico em %.0fs descartado: conversa com o chat/subs (%d sinais)", p, content["chat_talk"])
                    continue
            if chat_only(comps):
                # só o chat subiu (spam de emote/inscrições), sem grito nem reação na fala: raramente viraliza
                final_score *= float(self.d.get("chat_only_penalty", 0.8))
                if final_score < min_score:
                    log.info("pico em %.0fs descartado: só chat, sem reação (nota %.0f)", p, final_score)
                    continue
            if self.llm.enabled and final_score >= float(self.d.get("llm", {}).get("min_score_to_call", 40)):
                sample = [m["text"] for m in messages if seg_chat_start + start_rel <= m["ts"] <= seg_chat_start + end_rel][:12]
                llm = self.llm.score(stream["streamer"], text, sample)
                if llm:
                    comps["llm"] = llm
                    final_score = 0.75 * final_score + 0.25 * (llm["score"] * 10)
                    if llm["score"] <= 1:
                        log.info("LLM rejeitou pico em %.0fs: %s", p, llm.get("reason"))
                        continue
            comps["notes"] = self._reasons_text(comps)
            mid = self.db.add_moment(stream["id"], seg_id, start_ts, end_ts, segment["start_ts"] + p,
                                     round(final_score, 1), comps, text, words_abs)
            created.append(mid)
            log.info("momento %d: %.1f-%.1fs nota %.0f (%s)", mid, start_rel, end_rel, final_score, "; ".join(comps["notes"]))
        self.db.update("segments", seg_id, status="detected")
        return created

    @staticmethod
    def _reasons_text(c: dict) -> list[str]:
        notes = []
        if c.get("chat") is not None and c["chat"] >= 0.5:
            notes.append("chat explodiu")
        if c.get("audio", 0) >= 0.5:
            notes.append("pico de áudio/gritaria")
        if c.get("transcript", 0) >= 0.4:
            notes.append("reação forte na fala")
        if c.get("keywords_found"):
            notes.append("GTA: " + ", ".join(c["keywords_found"][:3]))
        content = c.get("content") or {}
        if content.get("viral"):
            notes.append("reação: " + ", ".join(content["viral"][:3]))
        if content.get("chat_talk", 0) >= 2:
            notes.append(f"conversa com o chat ({content['chat_talk']} sinais)")
        if c.get("llm"):
            notes.append(f"LLM {c['llm']['score']:.0f}/10: {c['llm'].get('reason', '')}")
        return notes or ["sinal combinado acima do limiar"]
