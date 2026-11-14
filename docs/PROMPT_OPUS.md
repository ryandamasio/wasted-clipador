# PROMPT PARA O OPUS — PROJETO "CLIPADOR" (Cortes GTA 6)

Copie TUDO abaixo da linha e cole numa nova sessão do Claude Code, aberta na pasta `D:\PROJETO CLIPADOR`.

---

## 0. PAPEL E POSTURA

Você é o arquiteto-chefe e engenheiro líder deste projeto. Trate-o como um produto de uma empresa profissional: código limpo, modular, testado, documentado, com tratamento de erros e logs. Pense como analista crítico: antes de cada decisão importante, considere o que pode dar errado e escolha a opção mais robusta e simples.

Regras de trabalho:
- Responda em português do Brasil. Código e nomes de arquivos em inglês.
- Economize tokens: não pesquise GitHub nem skills sem necessidade. Use o que você já sabe (Streamlink, yt-dlp, ffmpeg, faster-whisper, etc.). Pesquise na web só se uma API ou biblioteca específica estiver em dúvida.
- Faça no máximo UMA rodada de perguntas no início (seção 12) e depois execute sem ficar pedindo confirmação a cada passo.
- Construa em fases. Cada fase deve rodar de ponta a ponta antes da próxima começar. Nunca deixe o projeto num estado quebrado.
- Ao fim de cada fase, rode testes reais e me diga o que funcionou, o que não funcionou e o que falta.

## 1. VISÃO DO PRODUTO

Um programa para Windows 11 que:
1. Monitora e captura lives de vários streamers (Twitch, Kick e YouTube), até ~3 simultâneas.
2. Detecta automaticamente os momentos com potencial viral.
3. Corta, edita (vertical 9:16, legenda dinâmica, zoom, marca d'água própria) e prepara os clipes.
4. Gera títulos, descrições e hashtags diferentes por rede.
5. Coloca os clipes numa fila de aprovação rápida (celular ou navegador).
6. Agenda e posta no YouTube Shorts, TikTok, Instagram Reels (e Kwai, se viável) 3–5 vezes por dia.

Contexto de negócio: o GTA 6 deve ser um dos maiores lançamentos da história. O perfil será do nicho "Cortes GTA 6", com clipes de vários streamers grandes jogando. A vantagem competitiva é **velocidade** (postar em 15–30 min após o momento), **qualidade consistente** e **volume**, com automação máxima.

Streamers iniciais: Coringa, Pedro (amigo do dono), David Jones, mais outros que o dono incluir. Lista configurável em arquivo (`streamers.yaml`).

## 2. AMBIENTE E RESTRIÇÕES

- Windows 11 Pro, shell bash (Git Bash), pasta do projeto `D:\PROJETO CLIPADOR`.
- GPU: **NVIDIA GTX 1660, ~6 GB de VRAM** (confirmar com `nvidia-smi`). Use aceleração por GPU: faster-whisper com CUDA e ffmpeg com NVENC. Com 6 GB, escolha modelo Whisper `small` ou `medium` com quantização (int8/int8_float16), evite `large`, e não rode Whisper e encode pesado ao mesmo tempo sem controle de fila (limitar concorrência para não estourar VRAM).
- Linguagem principal: Python 3.11+. Ambiente virtual (`venv`) e `requirements.txt` fixando versões.
- Tudo roda local. Custos de API de IA devem ser mínimos: usar Whisper local e, se precisar de LLM, um modelo barato (ex.: Haiku) só para pontuar texto e gerar títulos.
- O projeto é desenvolvido por uma pessoa (o dono). Mantenha a organização simples.

## 3. ARQUITETURA (módulos independentes)

Crie esta estrutura (ajuste se encontrar algo melhor, justificando):

```
PROJETO CLIPADOR/
├─ CLAUDE.md                 # contexto do projeto para qualquer Claude que abrir a pasta
├─ README.md
├─ .env.example              # chaves/tokens (NUNCA commitar o .env real)
├─ .gitignore
├─ requirements.txt
├─ config/
│  ├─ streamers.yaml         # streamers, plataforma, canal, prioridade
│  ├─ settings.yaml          # limites, pesos de detecção, horários de postagem, etc.
│  └─ hashtags.yaml          # bancos de hashtags por rede
├─ clipador/
│  ├─ capture/               # módulo 1: captura das lives
│  ├─ detect/                # módulo 2: detecção de momentos
│  ├─ edit/                  # módulo 3: corte e edição
│  ├─ copy/                  # módulo 4: títulos, descrições, hashtags
│  ├─ review/                # módulo 5: fila de aprovação (web local)
│  ├─ publish/               # módulo 6: agendamento e postagem
│  ├─ store/                 # banco SQLite + modelos de dados
│  └─ orchestrator.py        # liga tudo, roda como serviço
├─ assets/
│  ├─ music_free/            # músicas livres de direitos (com licença registrada)
│  ├─ fonts/
│  └─ watermark/
├─ data/                     # gravações, clipes, banco (fora do git)
├─ tests/
└─ scripts/                  # utilitários: setup, healthcheck, limpeza de disco
```

Comunicação entre módulos via banco SQLite (tabelas: `streams`, `segments`, `moments`, `clips`, `posts`) para que cada módulo possa ser rodado e testado sozinho e retomado após falha.

## 4. MÓDULO 1 — CAPTURA

- Ferramenta principal: **Streamlink** (Twitch, Kick quando suportado) e **yt-dlp** (YouTube e fallback). Gravar em **segmentos de 5 minutos** (`.ts`/`.mp4`) sem recodificar, para: (a) processar enquanto a live ainda rola, (b) não perder tudo se algo cair.
- Monitor de live: checar a cada ~60 s quais streamers da lista estão online e iniciar/parar a gravação sozinho.
- Até 3 capturas simultâneas, configurável. Reconexão automática se cair.
- **Plano B:** captura por OBS (WebSocket) / gravação de tela se Streamlink falhar (especialmente Kick). Implementar ao menos a interface e documentar.
- **Chat:** em paralelo, gravar o chat da live com timestamps (Twitch IRC anônimo; Kick/YouTube chat quando possível). O chat é o **sinal nº 1** de momento viral.
- Controle de disco: política de retenção (apagar gravações brutas depois de X horas se já processadas e sem clipe pendente). Alertar quando faltar espaço.
- Cuidado com o atraso: registrar o timestamp absoluto de cada segmento para alinhar chat e vídeo.

## 5. MÓDULO 2 — DETECÇÃO DE MOMENTOS

Pontuação composta (0–100) por janela deslizante, com pesos em `settings.yaml`:
1. **Velocidade do chat** (mensagens/segundo vs. média móvel; picos, emotes de risada/choque, repetição de spam tipo "KKKKK", "?????", "PQP").
2. **Áudio**: picos de volume/energia, gritaria, risada (análise de loudness/RMS com ffmpeg/librosa).
3. **Transcrição** local com faster-whisper (modelo adequado à VRAM) → trechos enviados a um LLM barato que dá nota de 0–10 para "potencial de clipe" (engraçado, chocante, épico, reação forte, momento de GTA 6 relevante) e sugere ponto de início/fim ideal.
4. **Contexto GTA 6**: palavras-chave e eventos do jogo (perseguição, polícia, crash, assalto, easter egg, reação a gráfico/física, etc.) com bônus de pontuação. Lista editável.
5. **Anti-sensacionalismo**: não elevar nota por conteúdo ofensivo, discurso de ódio, conteúdo sexual, ou clipes fora de contexto que possam gerar problema com a plataforma ou prejudicar o streamer. Filtrar.

Saída: lista de "momentos" com início, fim, nota, motivo e trecho de transcrição. **Regras de recorte:** o clipe deve ter **gancho nos primeiros 2 segundos** (começar já no momento forte ou imediatamente antes, nunca com enrolação), duração alvo **15–40 s** (máximo configurável), sem cortar frase no meio.

Deduplicação: não gerar dois clipes quase iguais do mesmo momento.

## 6. MÓDULO 3 — EDIÇÃO (ffmpeg + GPU)

Para cada momento aprovado pela detecção, gerar vídeo final:
- **Formato 9:16 (1080x1920)**. Layout: gameplay principal em cima/centro com **blur do próprio vídeo ao fundo** ou, quando houver facecam, layout dividido (facecam + gameplay). Detectar/definir a posição da facecam por streamer em `streamers.yaml` (região configurável).
- **Legendas dinâmicas** (estilo palavra por palavra, destaque na palavra atual, fonte grande e legível), geradas pelo Whisper com timestamps por palavra. Português do Brasil. Revisão leve de erros comuns de gírias.
- **Zoom dinâmico** (punch-in) em picos de áudio/momento forte, suave, sem exagero.
- **Marca d'água própria**, pequena e semitransparente num canto que não cubra legenda nem facecam. Imagem configurável em `assets/watermark/`. Não colocar logo de outras redes.
- **Crédito do streamer** na tela (texto discreto "Clipe: @streamer") e na legenda do post.
- **Áudio:** normalizar volume (loudnorm). **Música:** NÃO embutir música com copyright no arquivo. Duas estratégias:
  - YouTube Shorts: usar apenas músicas de `assets/music_free/` (livres de direitos), em volume baixo ("ducking" sob a voz). Manter um `LICENSES.md` com origem e licença de cada faixa.
  - TikTok/Instagram: exportar versão **sem música** e indicar na ficha do clipe qual som trending o dono deve adicionar dentro do app na hora de postar (ou deixar esse passo manual).
- Exportar com NVENC (H.264), 30 fps, bitrate adequado para Shorts/TikTok/Reels. Gerar também thumbnail/frame de capa.
- **Exportar limpo**: sem marca d'água de outras plataformas (TikTok/Reels penalizam).
- Variação: pequenas variações visuais entre posts (posição de legenda, estilo) para não parecer conteúdo repetitivo de fábrica.

## 7. MÓDULO 4 — TÍTULOS, DESCRIÇÕES E HASHTAGS

- Para cada clipe, gerar **3 variações de título** (curtas, com gancho, sem clickbait enganoso nem sensacionalismo exagerado), descrição e hashtags **diferentes por rede** (YouTube, TikTok, Instagram, Kwai).
- Hashtags: mistura de grandes (#gta6, #gta, #rockstar), médias e do streamer/nicho. Máximo adequado por rede. Banco em `hashtags.yaml`, atualizável.
- Sempre incluir crédito: "Clipe de [streamer] — [link do canal]".
- Idioma: português do Brasil, tom de cortes (informal, mas sem exageros).
- Nada de promessas falsas ou informação inventada sobre o jogo. Se o clipe não tiver contexto claro, o título descreve o que acontece de forma neutra.

## 8. MÓDULO 5 — FILA DE APROVAÇÃO

- Mini aplicativo web local (FastAPI ou Flask, interface simples e **responsiva para celular**), acessível na rede local (e opcionalmente via túnel seguro como Tailscale ou Cloudflare Tunnel).
- Cada clipe aparece com: player do vídeo, nota, motivo da detecção, streamer, 3 títulos (escolher/editar), descrição e hashtags por rede, botões **Aprovar / Rejeitar / Ajustar corte (início/fim) / Re-editar**.
- Meta: aprovar um clipe em 5–10 segundos.
- Guardar a decisão do usuário (aprovado/rejeitado e por quê) para **calibrar a detecção** com o tempo.
- Modo "auto-aprovar" ativável por nota mínima (desligado por padrão) para quando a qualidade estiver confiável.

## 9. MÓDULO 6 — AGENDAMENTO E POSTAGEM

O amigo do dono já tem uma automação de postagem funcionando (YouTube, TikTok, Instagram). **Pergunte na seção 12 como ela funciona** e reaproveite/integre em vez de reinventar.

Requisitos:
- Interface de publicação por rede (`Publisher` com implementações: YouTube Data API, TikTok, Instagram Graph API, Kwai manual). Cada uma pode ser habilitada/desabilitada.
- **Modo manual primeiro** (essencial no MVP): o sistema entrega o clipe pronto + textos copiáveis e o dono posta à mão. A postagem automática vem depois.
- **Agendador**: 3–5 posts/dia, horários espalhados (com leve aleatoriedade), limites por conta, prioridade para momentos recentes (velocidade é crucial). Respeitar quotas das APIs.
- Anti-spam: não postar o mesmo vídeo em todas as redes no mesmo minuto; variar textos; começar com 2–3 posts/dia por conta nova e subir; permitir "aquecimento" de conta.
- Registrar tudo em `posts` (status, URL, erros) e fazer retry com backoff. Se uma rede falhar, as outras seguem.
- Segredos só em `.env`. Nunca em código nem no git.

## 10. RISCOS A MITIGAR (implementar mitigações, não só documentar)

1. **Música com copyright** → seção 6 (sem música embutida; só livres de direitos no YouTube).
2. **Conteúdo reaproveitado / desmonetização** → valor agregado em cada clipe (corte editado, legenda, zoom, título próprio, crédito) e sem repostar vídeo inteiro.
3. **Spam/automação detectada** → seção 9 (variação, horários, aquecimento, volume gradual).
4. **Qualidade irregular** → fila de aprovação + aprendizado com decisões.
5. **Captura quebrando (Kick/Twitch)** → plano B por OBS/tela, logs claros, healthcheck.
6. **Ponto único de falha** → contas reservas, backup do banco, recuperação após queda do PC.
7. **Disco cheio** → retenção automática.
8. **Concorrência no lançamento (muitos clipadores)** → priorizar velocidade: pipeline processa segmentos enquanto a live rola; meta de clipe pronto em < 10 min após o momento.
9. **Conteúdo problemático** (ofensivo, discurso de ódio, etc.) → filtros e revisão humana.
10. **Pedido de remoção por streamer** → comando simples para remover/blacklistar clipe ou streamer.

## 11. ORGANIZAÇÃO DO PROJETO (uso individual)

- Inicialize o repositório **git** (a pasta ainda não é repo) com `.gitignore` (venv, data/, .env, gravações, clipes, caches), para ter histórico e poder voltar atrás.
- Crie `CLAUDE.md` com: visão, arquitetura, convenções, como rodar e status de cada módulo, para que qualquer sessão futura comece já com contexto e sem gastar token reexplicando.
- Mantenha contratos claros entre módulos (esquema do SQLite e formato dos arquivos), assim o projeto fica fácil de evoluir ou de abrir a outras pessoas no futuro, sem exigir isso agora.

## 12. PERGUNTAS INICIAIS (faça TODAS numa única mensagem, depois execute)

1. De quais plataformas são os streamers (Twitch/Kick/YouTube) e quais são os canais exatos?
2. Como o amigo posta hoje (API própria? ferramenta pronta?) — dá para eu ver o código/ferramenta?
3. (Já respondida, não perguntar.) Hardware: AMD Ryzen 5 5500 (6 núcleos), ~16 GB RAM, GTX 1660 6 GB, disco D: com ~583 GB livres (o projeto e todos os dados ficam no D:). O disco C: tem só ~45 GB livres, então não instale nada pesado nem guarde gravações no C:. Com 16 GB de RAM e CPU modesta, controle a concorrência: poucas tarefas pesadas ao mesmo tempo.
4. Já existem contas criadas e aquecidas em YouTube/TikTok/Instagram/Kwai? Nome do perfil ("Cortes GTA 6" ou outro)?
5. Quer a primeira versão só com postagem manual (recomendado)?
6. Tem chave de API da Anthropic para usar um modelo barato na pontuação/títulos, ou prefere tudo local?

Se alguma resposta não vier, assuma o padrão mais seguro e simples e registre a suposição.

## 13. FASES DE ENTREGA (prazo total: ~2 dias, eu tenho pouco tempo)

**FASE 1 — MVP (prioridade absoluta, deve rodar logo):**
captura (Streamlink + chat) → detecção (chat + áudio + Whisper) → corte vertical com legenda → pasta de saída com clipes prontos + arquivo de textos (títulos/hashtags). Postagem **manual**. Testar com uma live real movimentada (pode não ser de GTA 6) e ajustar pesos.

**FASE 2 — Qualidade e velocidade:**
zoom dinâmico, marca d'água, crédito, música livre (YouTube), fila de aprovação web no celular, geração de títulos por rede, deduplicação, retenção de disco.

**FASE 3 — Automação:**
integração de postagem (reaproveitando a do amigo), agendador com anti-spam, retries, métricas básicas por post.

**FASE 4 — Robustez e escala:**
plano B OBS, healthcheck, 3 lives simultâneas estáveis, auto-aprovação por nota, painel simples de métricas, documentação final.

Em cada fase: código + testes + exemplo de execução + resumo curto do que foi feito e do que falta. Se o tempo apertar, **corte funcionalidades da Fase 4 antes de comprometer a Fase 1**.

## 14. CRITÉRIOS DE SUCESSO

- Fase 1: dado o link de uma live, o programa produz clipes verticais legendados dos melhores momentos, sem intervenção, e eu consigo postá-los à mão.
- Tempo do momento ao clipe pronto: meta < 10 min.
- ≥ 60% dos clipes sugeridos aprovados por mim após calibragem.
- Nenhum segredo no repositório; projeto roda a partir do README em máquina limpa.
- Pipeline retoma sozinho após queda de rede/PC.

## 15. COMEÇE AGORA

1. Leia esta pasta (`D:\PROJETO CLIPADOR`) para ver o que já existe.
2. Faça a mensagem única de perguntas da seção 12.
3. Com as respostas, inicialize git, estrutura, `CLAUDE.md`, ambiente Python e dependências.
4. Execute a Fase 1 até funcionar de ponta a ponta; só então avance.
