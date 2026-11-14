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
.venv/Scripts/python -m clipador run          # fica rodando: grava quem entrar ao vivo, produz clipes e sobe a fila web
.venv/Scripts/python -m clipador review       # só a fila de aprovação (sem gravar), ex.: para revisar depois
.venv/Scripts/python -m clipador status       # o que foi detectado/produzido + taxa de aprovação por nota
.venv/Scripts/python -m clipador check        # quem está online agora
```
Clipes aparecem em `data/output/AAAA-MM-DD/<streamer>_<hora>_m<id>/`:
`clip.mp4` (1080x1920, sem música: TikTok/Reels/Kwai), `clip_youtube.mp4` (com música livre, se houver faixas),
`cover.jpg`, `post.md` (3 títulos + textos por rede, copiáveis) e `post.json`.

### Fila de aprovação (celular)
Com `run` ou `review` rodando, abra `http://IP-do-PC:8787` no celular (mesmo Wi-Fi; o IP aparece no `status`).
Cada clipe mostra player, nota, motivo, 3 títulos editáveis e textos por rede, com botões **Aprovar / Rejeitar /
Ajustar corte (±1-2 s no início/fim) / Re-editar**. Aprovados ficam na aba própria com botão "copiar" por rede e
"Marcar como postado". Cada decisão é guardada em `reviews` para calibrar a detecção.
Para acessar fora de casa use Tailscale ou Cloudflare Tunnel (não exponha a porta direto na internet).
`review.auto_approve_min_score: 85` em `settings.yaml` aprova sozinho acima da nota (desligado por padrão).

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
3. **Edição** (`clipador/edit`): fundo desfocado + vídeo centralizado (ou facecam em cima e gameplay embaixo
   quando o streamer tem `facecam` no `streamers.yaml`), zoom suave nos picos de áudio, legenda palavra-a-palavra
   com posição variável, crédito do streamer, marca d'água opcional (`assets/watermark/watermark.png`), loudnorm, NVENC.
4. **Textos** (`clipador/copy`): 3 títulos com gancho, descrição com crédito e hashtags por rede.
5. **Aprovação** (`clipador/review`): app web local responsivo; decisões alimentam a calibração.

Música: `clip.mp4` nunca tem música. Se houver faixas em `assets/music_free/` (registradas em `LICENSES.md`),
sai também `clip_youtube.mp4` com a música normalizada baixo e "ducking" sob a voz; atribuição exigida pela
licença entra sozinha na descrição do YouTube. Em TikTok/Reels adicione o som trending dentro do app.

Facecam: meça a região da câmera num print da live e preencha em fração da tela, ex.
`facecam: {x: 0.72, y: 0.02, w: 0.26, h: 0.30}` (canto superior direito, 26% de largura, 30% de altura).

## Status
- **Fase 1 (MVP):** implementada e calibrada. Testes: `pytest -q tests`.
- **Fase 2:** fila de aprovação web, zoom dinâmico, layout facecam, música livre, retenção de rejeitados: implementada.
- Fases 3-4 (postagem automática, plano B OBS, métricas): ver `CLAUDE.md`.

## Remover um clipe/streamer (pedido do streamer)
Coloque `enabled: false` no `streamers.yaml` e apague a pasta do clipe em `data/output`. Nada é publicado sem você.
