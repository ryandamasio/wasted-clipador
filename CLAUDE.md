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
.venv/Scripts/python -m clipador status
.venv/Scripts/python -m pytest -q tests
```

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
edit/      subtitles.py (ASS palavra-a-palavra com destaque), render.py (9:16 blur bg + ass + crédito drawtext +
           marca d'água opcional + loudnorm; NVENC -> libx264 fallback; MediaNotReady se live ainda não cobre o fim)
copy/      generator.py (3 títulos, descrição e hashtags por rede via templates + hashtags.yaml; post.md/post.json)
orchestrator.py  threads monitor/detect/edit/retention; recover() após queda; 1 Whisper e 1 encode por vez
__main__.py      CLI (run, check, process-file, record, status, healthcheck)
```
Fluxo de status: segments `recorded -> detecting -> detected -> deleted`; moments `new -> editing -> clipped`
(`waiting_media` enquanto a live não gravou o fim do clipe; `failed`, `dupe`, `rejected`).
Saída: `data/output/AAAA-MM-DD/<handle>_<HHMMSS>_m<id>/{clip.mp4, cover.jpg, subs.ass, post.md, post.json}`.

## Configuração
- `config/streamers.yaml`: lista; handles marcados TODO precisam de confirmação. `enabled: false` desliga.
- `config/settings.yaml`: pesos de detecção (`detect.weights`), `min_score`, janela, Whisper, estilo de legenda,
  encoder, `capture.chat_offset_seconds` (atraso vídeo vs. chat; calibrar por streamer).
- `config/hashtags.yaml`: bancos big/mid/niche + por streamer; limites por rede.
- `.env` (copiar de `.env.example`): `ANTHROPIC_API_KEY` opcional (ativa `detect.llm.enabled`).

## Status (atualize ao fim de cada fase)
- Fase 1 (MVP): código completo; testes unitários passam; healthcheck OK (NVENC, CUDA, Whisper small int8_float16
  carrega em ~22 s). Teste real em andamento (ver README > Status).
- Fase 2: zoom dinâmico, facecam split, música livre (YouTube), fila de aprovação web, retenção já existe (básica).
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
