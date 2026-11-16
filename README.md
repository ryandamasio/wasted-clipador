# WASTED — o clipador de lives

> *Wasted*: a tela que aparece no GTA quando o personagem cai. Aqui, é o momento em que a live "cai" no corte.

Clipes automáticos de lives para o nicho **Cortes GTA 6**: monitora streamers na Twitch, Kick e YouTube,
grava, detecta os melhores momentos (chat + áudio + transcrição), corta em 9:16 com legenda dinâmica, zoom e
crédito, passa por uma fila de aprovação no celular e agenda/publica nas redes (ou entrega tudo pronto para
você postar). O pacote Python continua se chamando `clipador`.

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
`clip.mp4` (1080x1920, sem música: Reels/Kwai), `clip_youtube.mp4` (com música livre), `clip_long.mp4`
(versão de 62-95 s para o TikTok, cujo programa de recompensas só paga vídeos com mais de 1 minuto; gerada quando a
gravação já cobre o trecho), `cover.jpg`, `post.md` (3 títulos + textos por rede, copiáveis) e `post.json`.
A interface web (`http://localhost:8787`, atalho WASTED na área de trabalho) mostra tudo isso por clipe.

### Fila de aprovação (celular)
Com `run` ou `review` rodando, abra `http://IP-do-PC:8787` no celular (mesmo Wi-Fi; o IP aparece no `status`).
Cada clipe mostra player, nota, motivo, 3 títulos editáveis e textos por rede, com botões **Aprovar / Rejeitar /
Ajustar corte (±1-2 s no início/fim) / Re-editar**. Aprovados ficam na aba própria com botão "copiar" por rede e
"Marcar como postado". Cada decisão é guardada em `reviews` para calibrar a detecção.
Para acessar fora de casa (você ou um amigo): defina `REVIEW_PASSWORD` no `.env` e deixe `review.tunnel: true`;
com o `cloudflared` instalado (`winget install Cloudflare.cloudflared`) o `run` abre um endereço
`https://xxxx.trycloudflare.com`, mostrado no `clipador status`, no log e no Painel. Usuário `wasted` + a senha.
O endereço muda a cada reinício; sem senha o túnel não abre.
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

### Publicação e agenda (Fase 3)
Ao aprovar um clipe, o agendador cria um post por rede habilitada dentro das janelas de `publish.windows`,
respeitando limite diário (com rampa de aquecimento para conta nova), espaçamento mínimo e defasagem entre
redes (nunca o mesmo vídeo em todas as redes no mesmo minuto). A aba **Agenda** da fila mostra o que postar e quando.

- `publish.mode: manual` (padrão): você posta pelo app da rede no horário sugerido, copia o texto na Agenda e toca
  em "Já postei". Nada sai sem você.
- `publish.mode: auto`: as redes com `via: api` publicam sozinhas (YouTube Data API, TikTok Content Posting API,
  Instagram Graph API com upload resumível) e `via: upload-post` usa o serviço Upload-Post com uma chave só.
  Credenciais só no `.env` (veja `.env.example`); tokens renovados ficam em `data/tokens/`. Retry com backoff;
  erros definitivos (credencial, vídeo rejeitado) não repetem. Kwai não tem API pública: fica manual.
```bash
.venv/Scripts/python -m clipador publish status            # redes configuradas, janelas, contadores
.venv/Scripts/python -m clipador publish auth youtube      # autoriza a conta (abre o navegador); idem tiktok
.venv/Scripts/python -m clipador publish list              # agenda
.venv/Scripts/python -m clipador publish run               # publica o que venceu (modo auto; `run` já faz isso sozinho)
.venv/Scripts/python -m clipador publish metrics           # views/likes dos posts via API
```
Limites a lembrar: YouTube gasta 1600 de 10.000 unidades de quota por upload (6/dia); TikTok só publica em
modo privado até o app passar pela auditoria; Instagram exige conta profissional ligada a uma Página.

## Robustez (Fase 4)

### Painel
Aba **Painel** da fila: lives agora, clipes por dia e por streamer, taxa de aprovação, latência momento → clipe,
fila do Whisper, disco, GPU, posts e views por rede, último backup.

### Rodar sozinho e voltar após queda
```bash
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1     # tarefa no logon (remova com -Remove)
scripts\run_forever.bat                                                     # ou só o loop que reinicia se cair
```
O estado fica no SQLite, então depois de uma queda o `run` retoma: segmentos não detectados voltam para a fila,
momentos em edição são renderizados de novo, posts agendados seguem. Backup diário do banco em `data/backups/`
(7 cópias). Configure o Windows para não suspender na tomada.

### Plano B: captura de tela
Se o streamlink e o yt-dlp falharem seguidas vezes (ex.: Kick bloqueando), o gravador pode abrir a live num Chrome
em modo kiosk numa região/monitor e gravar a tela com NVENC (`capture.plan_b`). Precisa de um dispositivo de áudio
loopback para o som: instale o [VB-Cable](https://vb-audio.com/Cable/) (saída padrão = CABLE Input, capture
"CABLE Output") ou o `virtual-audio-capturer` do screen-capture-recorder, preencha `plan_b.audio_device` com o nome
que o `healthcheck` lista e ligue `plan_b.enabled: true`. `force: true` serve para testar sem esperar falha.
Durante o plano B a tela escolhida fica ocupada pelo player.

### Teste de carga (3 lives ao mesmo tempo)
```bash
.venv/Scripts/python scripts/stress.py --minutes 8 --lives 3
```
Monta 3 "lives" a partir de gravações antigas (reproduzidas em tempo real), roda detecção e edição de verdade em
banco e pastas separados (`data/stress_*`) e imprime a fila do Whisper, latência e uso de GPU. Pare o `run` antes,
para a medição não dividir a GPU com as lives reais.

### Pedido de remoção
```bash
.venv/Scripts/python -m clipador remove --streamer coringa --reason "pediu" [--purge]   # bloqueia e rejeita tudo
.venv/Scripts/python -m clipador remove --clip 12 [--purge]                            # um clipe só
```
O handle vai para `config/blocklist.yaml` e nunca mais é gravado, mesmo com `enabled: true`.

## Status
- **Fase 1 (MVP):** implementada e calibrada. Testes: `pytest -q tests`.
- **Fase 2:** fila de aprovação web, zoom dinâmico, layout facecam, música livre, retenção de rejeitados: implementada.
- **Fase 3:** agendador anti-spam, publishers (YouTube/TikTok/Instagram/Upload-Post/manual), retries, métricas: implementada
  (APIs testadas com HTTP simulado; falta rodar com contas reais quando existirem).
- **Fase 4:** painel de métricas, plano B por captura de tela, auto-start/reinício, backup do banco, comando de
  remoção: implementada. Falta validar 3 lives simultâneas numa noite cheia e o plano B com áudio loopback instalado.

## Para colaborar (outra máquina)
```bash
git clone https://github.com/ryandamasio/wasted-clipador.git
cd wasted-clipador
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
copy .env.example .env
.venv/Scripts/python -m clipador healthcheck
```
Precisa de Windows com GPU NVIDIA para o Whisper em CUDA e o NVENC (sem GPU roda em CPU/libx264, bem mais lento).
Nunca commite `.env`, `data/` nem `tools/` (já estão no `.gitignore`). O banco e os clipes são locais de cada máquina;
quem roda o `run` é o PC que grava as lives. Combine quem está rodando para não gravar a mesma live duas vezes.

## Remover um clipe/streamer (pedido do streamer)
Coloque `enabled: false` no `streamers.yaml` e apague a pasta do clipe em `data/output`. Nada é publicado sem você.
