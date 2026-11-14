# Clipador

Clipes automáticos de lives para o nicho **Cortes GTA 6**: monitora streamers na Twitch, Kick e YouTube,
grava, detecta os melhores momentos (chat + áudio + transcrição), corta em 9:16 com legenda dinâmica e entrega
o clipe pronto com títulos, descrição e hashtags por rede para você postar.

## Requisitos
- Windows 11, Python 3.12, Git Bash.
- ffmpeg no PATH (`winget install Gyan.FFmpeg`), com NVENC (GPU NVIDIA) — cai para libx264 se não houver.
- GPU NVIDIA com driver atual (Whisper roda em CUDA; cai para CPU se falhar).
- Chrome ou Edge instalados (a Kick exige navegador para liberar a API).

## Instalação
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env            # opcional: ANTHROPIC_API_KEY para pontuação/títulos com LLM
.venv/Scripts/python -m clipador healthcheck --whisper
```
Edite `config/streamers.yaml` (canais) e, se quiser, `config/settings.yaml` (pesos, legenda, encoder).

## Uso
```bash
.venv/Scripts/python -m clipador run          # fica rodando: grava quem entrar ao vivo e produz clipes
.venv/Scripts/python -m clipador status       # o que foi detectado/produzido
.venv/Scripts/python -m clipador check        # quem está online agora
```
Clipes aparecem em `data/output/AAAA-MM-DD/<streamer>_<hora>_m<id>/`:
`clip.mp4` (1080x1920), `cover.jpg`, `post.md` (3 títulos + textos por rede, copiáveis) e `post.json`.

Testes sem esperar uma live:
```bash
.venv/Scripts/python -m clipador process-file video.mp4 --streamer coringa          # VOD/arquivo local
.venv/Scripts/python -m clipador record --streamer gaules --minutes 6               # grava N min de uma live
```

## Como funciona
1. **Captura** (`clipador/capture`): streamlink (ou yt-dlp) → ffmpeg copia em segmentos `.ts` de 2 min, sem
   recodificar; chat gravado em paralelo com timestamps. Até 3 lives ao mesmo tempo. Reconecta se cair.
2. **Detecção** (`clipador/detect`): por segmento, calcula sinais por segundo (explosão do chat, picos de volume,
   reação na fala via faster-whisper, palavras-chave de GTA) e combina em nota 0-100. Picos acima de `min_score`
   viram momentos com início no gancho e fim em pausa de fala (15-40 s). Deduplica. Filtra termos problemáticos.
3. **Edição** (`clipador/edit`): fundo desfocado + vídeo centralizado, legenda palavra-a-palavra, crédito do
   streamer, marca d'água opcional (`assets/watermark/watermark.png`), loudnorm, NVENC.
4. **Textos** (`clipador/copy`): 3 títulos com gancho, descrição com crédito e hashtags por rede.

Música: nunca é embutida. Para YouTube use faixas de `assets/music_free/` (registre em `LICENSES.md`);
em TikTok/Reels adicione o som trending dentro do app.

## Status
- **Fase 1 (MVP):** implementada. Testes: `pytest -q tests`.
- Fases 2-4 (fila de aprovação web, zoom, música livre, postagem automática, plano B OBS): ver `CLAUDE.md`.

## Remover um clipe/streamer (pedido do streamer)
Coloque `enabled: false` no `streamers.yaml` e apague a pasta do clipe em `data/output`. Nada é publicado sem você.
