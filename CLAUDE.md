# Clipador — contexto do projeto

Automação de cortes de lives (nicho "Cortes GTA 6") para Windows 11: captura lives (Twitch/Kick/YouTube),
detecta momentos virais, gera clipes verticais legendados e textos de post. Uso individual (dono: Ryan).
Responder em pt-BR; código, nomes de arquivos e commits em inglês. Spec completa em `docs/PROMPT_OPUS.md`.

## Ambiente
- Windows 11, Git Bash, Python 3.12 em `.venv` (`.venv/Scripts/python`). GPU GTX 1660 6 GB (CUDA via pacotes pip
  `nvidia-cublas-cu12`/`nvidia-cudnn-cu12`; sem CUDA toolkit). ffmpeg 9 (winget) com `h264_nvenc` e libass.
- Disco C: quase cheio: tudo (dados, modelos Whisper via `HF_HOME=data/models`, cache pip) fica em D:.
- Heredoc bash com aspas misturadas quebra nesta máquina: para escrever código use a ferramenta Write/Edit.
- Nunca commitar `.env` nem `data/`.

## Comandos
```bash
.venv/Scripts/python -m clipador healthcheck --whisper   # ffmpeg, NVENC, CUDA, navegador, disco, Whisper
.venv/Scripts/python -m clipador check --all             # quem está online
.venv/Scripts/python -m clipador run                     # serviço completo
.venv/Scripts/python -m clipador record --streamer gaules --minutes 6   # teste real curto
.venv/Scripts/python -m clipador process-file VIDEO.mp4 --streamer coringa [--chat chat.jsonl]
.venv/Scripts/python -m clipador status                  # inclui taxa de aprovação por faixa de nota
.venv/Scripts/python -m clipador review [--port 8787]    # só a fila de aprovação web (+ worker de re-render)
.venv/Scripts/python -m pytest -q tests
```
Preview da fila no app: `.claude/launch.json` (nome `review`). Se a porta 8787 já estiver em uso, é porque
um `clipador run` já está rodando e serve a fila; use essa instância em vez de subir outra.

## Arquitetura (contrato = SQLite `data/clipador.sqlite`, schema em `clipador/store/db.py`)
```
capture/   monitor.py (online check: streamlink -> yt-dlp fallback; navegador p/ Kick)
           recorder.py (streamlink|yt-dlp --stdout -> ffmpeg -c copy -f segment .ts de N s; registra `segments`
                        com epoch absoluto start_ts/end_ts; reconexão; alterna fonte quando falha)
           chat.py (Twitch IRC anônimo, Kick Pusher websocket, YouTube chat-downloader -> `chat_messages`)
detect/    audio.py (RMS/dB -> sinal 0..1 por segundo), transcribe.py (faster-whisper singleton + lock, CUDA->CPU
           fallback), signals.py (chat, transcrição, keywords GTA, blocklist), scorer.py (combinação ponderada
           com renormalização de pesos quando falta sinal; picos; refine_bounds = gancho no pico sem cortar frase),
           llm.py (Haiku opcional, desligado), pipeline.py (Detector.detect_segment -> `moments`, dedupe)
edit/      subtitles.py (ASS palavra-a-palavra com destaque), render.py (9:16 blur bg + zoompan nos picos de áudio
           [pick_zoom_peaks/zoom_expression] + layout center|split (facecam do streamers.yaml, _layout) + ass +
           crédito drawtext + marca d'água opcional + loudnorm; NVENC -> libx264 fallback; MediaNotReady se a live
           ainda não cobre o fim; _music_version: clip_youtube.mp4 = vídeo copiado + música de assets/music_free
           normalizada (loudnorm I=-27) com sidechaincompress pela voz; LICENSES.md lido p/ atribuição)
copy/      generator.py (3 títulos, descrição e hashtags por rede via templates + hashtags.yaml; post.md/post.json;
           music_note/atribuição; chosen_title ao aprovar)
review/    service.py (ReviewService: list/approve/reject/recut/reedit/restore/mark_posted/purge_old; grava `reviews`),
           app.py (FastAPI + ReviewServer em thread; /api/clips, /media/{id}/{clip.mp4|clip_youtube.mp4|cover.jpg}),
           templates/index.html (página única responsiva, JS puro)
orchestrator.py  threads monitor/detect/edit/retention + ReviewServer; recover() após queda; 1 Whisper e 1 encode
           por vez; auto-aprovação por nota (review.auto_approve_min_score); MediaNotReady com live encerrada -> failed
__main__.py      CLI (run, check, process-file, record, status, review, healthcheck)
```
Fluxo de status: segments `recorded -> detecting -> detected -> deleted`; moments `new -> editing -> clipped`
(`waiting_media` enquanto a live não gravou o fim do clipe; `failed`, `dupe`, `rejected`); clips
`ready -> approved -> posted` (`rejected`, `superseded` após recorte/re-edição, `purged` após review.purge_after_hours).
Recorte na fila: ajusta start/end do momento, status volta a `new`, o edit loop renderiza um clipe novo (mesmo moment_id).
Saída: `data/output/AAAA-MM-DD/<handle>_<HHMMSS>_m<id>/{clip.mp4, clip_youtube.mp4?, cover.jpg, subs.ass, post.md, post.json}`.

## Configuração
- `config/streamers.yaml`: lista; handles marcados TODO precisam de confirmação. `enabled: false` desliga.
- `config/settings.yaml`: pesos de detecção (`detect.weights`), `min_score`, janela, Whisper, estilo de legenda,
  encoder, `capture.chat_offset_seconds` (atraso vídeo vs. chat; calibrar por streamer).
- `config/hashtags.yaml`: bancos big/mid/niche + por streamer; limites por rede.
- `.env` (copiar de `.env.example`): `ANTHROPIC_API_KEY` opcional (ativa `detect.llm.enabled`).

## Status (atualize ao fim de cada fase)
- Fase 1 (MVP): CONCLUÍDA e validada em 14/11/2026. Testes unitários passam. Healthcheck OK. Whisper small
  int8_float16 transcreve 120 s em 4-6 s na GTX 1660. Teste real (`record` 6 min do Gaules): captura, segmentos,
  chat IRC e detecção OK (sem momentos: rerun calmo, chat vazio). Teste `process-file` num vídeo de 4 min:
  clipe 1080x1920 renderizado com NVENC em 19 s, legenda/crédito corretos (ver data/output/2026-11-14).
  Calibração inicial (14/11, lives calmas: Gaules rerun, Brederodess/Tibia 3,5k viewers, 91 msgs/10 min):
  p99 das notas por segundo ~45 -> `min_score` 50, pesos chat .50/audio .20/transcript .15/keywords .15,
  `chat_offset_seconds` 7, piso de rajada `min_burst_rate` 1.5 msg/s, lista `sensitive_words` (x0.7).
  Com esses valores nenhuma das lives calmas gera clipe (correto). FALTA validar numa live movimentada
  (Coringa/GTA) e rever: `scripts/calibrate.py --stream N` recalcula tudo offline a partir do cache.
- Fase 2: CONCLUÍDA em 14/11/2026. Fila de aprovação web (FastAPI, porta 8787, responsiva; aprovar/rejeitar/
  ajustar corte ±s/re-editar/restaurar/marcar postado; decisões em `reviews`; auto-aprovação opcional), zoom
  dinâmico (zoompan, 12%, até 3 picos/clipe), layout facecam+gameplay por `facecam` no streamers.yaml, música livre
  só em clip_youtube.mp4 (loudnorm -27 LUFS + ducking; LICENSES.md), purga de rejeitados após 72 h, MediaNotReady
  com live encerrada vira failed. Validado: render center/split com zoom em 21-26 s (NVENC), recorte pela interface
  re-renderizou em 30 s, aprovação reescreveu post.md. 20 testes passam. Sem faixas em assets/music_free ainda
  (dono precisa baixar e registrar); facecam de todos os streamers ainda null (medir nas lives).
  Contexto: GTA 6 só lança em 19/11, então não há live de GTA 6 para calibrar; usar GTA RP (Coringa) como proxy.
- Fase 3: publicação automática (`publish/`), agendador anti-spam. Candidatos: Upload-Post API (via OpenShorts),
  APIs oficiais (YouTube Data v3, Instagram Graph, TikTok Content Posting). Automação do amigo do dono: desconhecida.
- Fase 4: plano B OBS, 3 lives simultâneas estáveis, auto-aprovação, métricas.

## Decisões registradas
- Streamers iniciais por pesquisa (Streams Charts 2025): Coringa (Kick, nº1 em GTA BR), PaulinhoLOKObr, Gaules,
  Baiano, Alanzoka. "David Jones" do pedido provavelmente é Davy Jones (Gameplayrj), desabilitado até confirmar.
- Kick: plugin nativo do streamlink exige navegador Chromium (Chrome detectado). API do chat da Kick pode ser
  bloqueada por Cloudflare: detecção segue sem chat (pesos renormalizam).
- Sem LLM por padrão (sem chave); postagem manual no MVP; contas de redes ainda não criadas.
- GTA 6 lança em 19/11/2026.
