# TeleAgent

Agente de IA controlado pelo Telegram que usa o **Claude Code CLI** instalado na sua máquina para trabalhar
em projetos reais, mantém **memória persistente por projeto** e permite **continuar o trabalho em outro
computador** por meio de um repositório Git privado.

- Somente o proprietário (ID numérico do Telegram) pode usar o bot.
- O Claude Code roda com suas proteções ativas; ações sensíveis viram **pedidos de confirmação** no Telegram.
- Sem banco de dados, sem serviços pagos: Python + arquivos JSON/Markdown + Git.

---

## Sumário

1. [Arquitetura](#arquitetura)
2. [Requisitos](#requisitos)
3. [Instalação e configuração](#instalação-e-configuração)
4. [Uso pelo Telegram](#uso-pelo-telegram)
5. [Memória persistente](#memória-persistente)
6. [Usando em dois computadores](#usando-em-dois-computadores)
7. [Segurança](#segurança)
8. [Configurações (.env)](#configurações-env)
9. [Testes](#testes)
10. [Solução de problemas](#solução-de-problemas)
11. [Limitações conhecidas](#limitações-conhecidas)

---

## Arquitetura

```
 Telegram (você) ──► Bot API ──► TeleAgent (python -m teleagent run)
                                   │
                                   ├─ bot.py ............ comandos, autenticação do dono, confirmações
                                   ├─ claude_runner.py .. `claude -p --output-format stream-json` (subprocesso, sem shell)
                                   ├─ memory.py ......... memória por projeto (state.json + MEMORY.md)
                                   ├─ gitsync.py ........ sincronização Git da memória (mescla semântica)
                                   ├─ instance_lock.py .. trava local + lease entre máquinas + detecção de 409
                                   ├─ deps.py ........... npm/pnpm/yarn/bun/pip/uv/poetry/pdm/pipenv/bundler/go/cargo/composer
                                   └─ security.py ....... caminhos autorizados, redação de segredos, análise de negações
```

```
teleagent/                 código
  __main__.py              CLI: run | setup | check
  bot.py                   handlers do Telegram e orquestração
  claude_runner.py         integração com a CLI do Claude Code
  config.py                .env, listas de ferramentas permitidas/negadas, detecção do executável claude
  memory.py                memória estruturada + renderização Markdown
  gitsync.py               sync Git da memória + Git seguro em projetos
  instance_lock.py         trava de instância
  deps.py                  detecção/instalação de dependências
  security.py              validações e redação
  storage.py               estado local da máquina (não sincronizado)
  messaging.py             divisão de mensagens longas
  logging_setup.py         logs rotativos com redação de segredos
  setup_wizard.py          assistente de configuração
  diagnostics.py           `python -m teleagent check`
tests/                     testes automatizados (pytest)
data/                      (gerado, ignorado pelo Git) estado local + clone do repositório de memória
logs/                      (gerado, ignorado pelo Git) teleagent.log e logs de cada tarefa
start.sh / start.bat       inicialização (Linux/macOS / Windows)
setup.sh / setup.bat       assistente de configuração
```

### Como uma tarefa é executada

1. Você envia um texto ao bot com um projeto selecionado.
2. O TeleAgent monta um prompt com as regras da sessão, a **memória do projeto** e a sua tarefa, e chama:
   ```
   claude -p --output-format stream-json --verbose \
          --permission-mode dontAsk --permission-prompts none \
          --allowedTools <lista segura> --disallowedTools <lista destrutiva> \
          [--resume <sessão>] [--model ...] [--max-budget-usd ...]
   ```
   O prompt vai pela entrada padrão; o diretório de trabalho é o do projeto.
3. O progresso (ferramentas usadas, último texto) é mostrado editando uma mensagem a cada ~5 s.
4. No fim, o Claude devolve um bloco `<teleagent-memory>{json}</teleagent-memory>` que atualiza a memória
   (decisões, concluídos, pendências, próximos passos). O bloco é removido da resposta enviada a você.
5. Se o Claude tentou algo não pré-aprovado, o Claude Code **negou** e o TeleAgent mostra a lista com os
   botões **Aprovar e continuar** / **Recusar**. A aprovação retoma a **mesma sessão** liberando
   **somente os comandos exatos** aprovados.
6. A memória é commitada e enviada ao repositório Git privado.

> A interface usada é a documentada da CLI (`claude --help`, versão testada: **2.1.307**). Os campos
> `permission_denials`, `session_id`, `result` e `total_cost_usd` do evento `result` em stream-json foram
> verificados numa execução real (veja [Testes](#testes)).

---

## Requisitos

| Item | Observação |
|---|---|
| Python 3.10+ | testado com 3.14.6 |
| Git | para sincronizar a memória e operar nos projetos |
| Claude Code CLI | instalado **e autenticado** em cada computador (`claude` → login, ou `claude auth login`) |
| Bot do Telegram | criado com o [@BotFather](https://t.me/BotFather) |
| Repositório Git **privado** e **vazio** | para a memória (GitHub, GitLab, Gitea…). Acesso por **SSH** recomendado |

Instalar o Claude Code: <https://docs.claude.com/en/docs/claude-code/setup>. No Windows use o instalador
nativo (gera `claude.exe`). Se o `claude` não estiver no PATH, o TeleAgent também procura o binário que
acompanha a extensão do Claude Code em VS Code/Cursor/Windsurf; ou defina `CLAUDE_BIN` no `.env`.

---

## Instalação e configuração

### 1. Preparar (uma vez, em qualquer computador)

1. **Bot**: no Telegram, fale com o @BotFather → `/newbot` → guarde o token.
2. **Repositório de memória**: crie um repositório **privado e vazio**, por exemplo `teleagent-memoria`.
3. **Chave SSH** com acesso a esse repositório em cada computador
   (`ssh-keygen -t ed25519`, adicione a chave pública na sua conta Git e teste com `ssh -T git@github.com`).
   Alternativa: HTTPS com o gerenciador de credenciais do Git. **Nunca** coloque usuário/senha/token na URL.

### 2. Instalar o TeleAgent

Linux/macOS:
```bash
cd teleagent            # pasta deste projeto
./setup.sh              # cria .venv, instala dependências, roda o assistente e o diagnóstico
./start.sh              # inicia o bot
```

Windows (PowerShell ou cmd):
```bat
cd teleagent
setup.bat
start.bat
```

### 3. O assistente de configuração (`python -m teleagent setup`)

Pergunta e valida:

1. **Token do bot** (digitação oculta, validado com `getMe`).
2. **Seu ID numérico** — detectado automaticamente: você envia uma mensagem ao bot e confirma que é você
   (ou digite o ID; o @userinfobot informa).
3. **Diretório(s) de projetos** autorizados (vários separados por `;`). Home e raiz do disco são recusados.
4. **URL do repositório privado** de memória (testada com `git ls-remote`).
5. **Nome da máquina** e detecção/login do Claude Code.

O resultado é gravado em `.env` (permissão `600` no Linux/macOS). Rode `python -m teleagent check` a qualquer
momento para um diagnóstico completo.

---

## Uso pelo Telegram

| Comando | O que faz |
|---|---|
| `/start` | Apresentação e projeto atual |
| `/help` | Ajuda |
| `/status` | Máquina, versão/login do Claude Code, projeto (git, memória, sessão), tarefa em andamento, sincronização, dono da instância |
| `/projetos` | Lista projetos com botões; `/projetos nome` seleciona direto. Projetos que só existem na memória aparecem com ☁️ e podem ser **clonados** (com confirmação) |
| `/criar nome` | Cria uma pasta de projeto nova (com várias pastas autorizadas, você escolhe onde por botão) e a seleciona |
| `/memoria` | Mostra a memória do projeto. `/memoria objetivo\|decisao\|pendencia\|proximo\|nota\|feito TEXTO` adiciona itens |
| `/continuar [instruções]` | Sincroniza a memória, faz `pull` seguro do projeto (só fast-forward, só com árvore limpa) e pede ao Claude para retomar pelas pendências/próximos passos |
| `/parar` | Cancela a tarefa em andamento (encerra a árvore de processos). Sem tarefa: oferece **desligar o bot nesta máquina** e liberar para outro computador |
| `/instalar` | Detecta gerenciadores (npm, pnpm, yarn, bun, pip/venv, uv, poetry, pdm, pipenv, bundler, go, cargo, composer) e instala após confirmação |
| `/sync` | Sincroniza a memória agora |
| `/publicar [mensagem]` | Mostra as alterações e, após confirmação, faz commit + push (nunca `--force`). Bloqueia se houver `.env`, chaves ou padrões de segredo |
| `/novo` | Esquece a sessão do Claude Code do projeto nesta máquina (a memória continua) |
| `/logs` | Final do log |
| `/agentes` | Lista os agentes de IA com botões para ver, usar, trocar motor/modelo, editar instruções ou apagar |
| `/agente novo NOME` | Cria um agente: escolhe o motor (**Claude Code** ou **OpenCode**), o modelo (Sonnet/Opus/Haiku ou os modelos 🆓 grátis do OpenCode, ou digita outro) e as instruções |
| `/agente usar\|modelo\|instrucoes\|apagar NOME` | Atalhos; `/agente nenhum` volta ao runner padrão |

**Agentes**: um agente é um perfil (motor + modelo + instruções). O agente em uso no projeto selecionado
define qual CLI roda a tarefa e com qual modelo; as instruções são acrescentadas ao prompt. Sem projeto
selecionado, “usar” define o agente padrão. Ficam em `data/agents.json` (local a cada máquina, não
sincronizado). Ao trocar de motor, a tarefa seguinte começa uma sessão nova (a memória do projeto continua).
Aprovações de ações negadas só funcionam com agentes Claude Code.

**Tarefas**: qualquer texto (que não seja comando) é enviado como tarefa ao Claude Code no projeto
selecionado. Uma tarefa por vez; mensagens enviadas durante uma tarefa são recusadas com aviso.

**Mensagens longas**: respostas são divididas em partes de até ~3900 caracteres; acima de 12 000
caracteres, chega um resumo e o texto completo como arquivo `.md`.

**Tarefas demoradas**: limite configurável (`TASK_TIMEOUT_SECONDS`, padrão 1 h). Ao estourar, a tarefa é
encerrada, registrada como pendência e pode ser retomada com `/continuar`.

**Interrupções**: se o processo cair ou a máquina reiniciar no meio de uma tarefa, ao voltar o TeleAgent
avisa no Telegram, registra a tarefa como `interrupted` e cria uma pendência “Retomar tarefa…”.

---

## Memória persistente

Fica no repositório de memória (`data/sync/` localmente):

```
memory/<projeto>/state.json   fonte da verdade (estruturada)
memory/<projeto>/MEMORY.md    visão legível, regenerada automaticamente
locks/instance.json           qual máquina está com o bot
```

Cada item (objetivo, decisão, pendência, próximo passo, concluído, nota) tem ID, data, máquina de origem e
status. Pendências e próximos passos são substituídos pela lista atual que o Claude informa ao fim de cada
tarefa; itens antigos ficam como `resolved` (não são apagados). O histórico guarda as últimas 200 tarefas.

Todo texto é passado por **redação de segredos** (tokens do Telegram, Anthropic, OpenAI, GitHub, GitLab,
Slack, AWS, Google, chaves privadas, `senha=...`, credenciais em URLs) antes de ser salvo, e o commit é
**bloqueado** se algo com cara de segredo ainda estiver no conteúdo preparado.

**O que NÃO é sincronizado**: `.env`, sessões e arquivos internos do Claude Code (`~/.claude`), logs de
tarefas (`logs/`), estado local (`data/local_state.json`). Por isso, ao trocar de computador, o contexto vem
da **memória**, não da sessão do Claude Code; no mesmo computador a sessão é retomada com `--resume`.

---

## Usando em dois computadores

Exemplo: **Computador A** (desktop) e **Computador B** (notebook). Só **um** pode fazer polling do bot por vez.

### Configuração inicial — Computador A

```bash
git clone <seu-repo-privado-do-teleagent> teleagent     # ou copie esta pasta
cd teleagent
./setup.sh            # Windows: setup.bat
#  - token do bot, seu ID, pasta de projetos (ex.: /home/voce/projetos)
#  - SYNC_REPO_URL = git@github.com:voce/teleagent-memoria.git
#  - MACHINE_NAME = desktop
./start.sh            # Windows: start.bat
```

No Telegram: `/projetos` → escolha o projeto → envie tarefas.

> **Compartilhar o código do TeleAgent** (opcional): esta pasta pode ser versionada num repositório privado
> próprio — o `.gitignore` já exclui `.env`, `data/`, `logs/` e `.venv/`:
> ```bash
> git init && git add . && git commit -m "TeleAgent"
> git remote add origin git@github.com:voce/teleagent.git && git push -u origin main
> ```
> **Código dos projetos**: cada projeto usa o próprio repositório Git. Publique com `/publicar` (ou peça ao
> agente e aprove o `git push`). O TeleAgent grava a URL do remoto (sem credenciais) na memória do projeto.

### Configuração inicial — Computador B

1. Instale Python, Git e o Claude Code; faça login no Claude Code (`claude`).
2. Configure a chave SSH com acesso ao repositório de memória (e aos repositórios dos projetos).
3. Obtenha o TeleAgent (`git clone` do seu repositório privado ou cópia da pasta, **sem** `.env`).
4. Rode `./setup.sh` / `setup.bat` com o **mesmo token**, o **mesmo ID**, o **mesmo SYNC_REPO_URL**,
   a pasta de projetos **deste** computador e outro nome (ex.: `notebook`).
5. **Não** inicie ainda se o Computador A estiver rodando (veja abaixo).

### Alternar de A para B (procedimento normal)

1. No Telegram, com nenhuma tarefa rodando: `/parar` → **⏻ Desligar aqui**.
   O Computador A sincroniza a memória, marca a instância como liberada (`locks/instance.json`) e encerra.
   (Ctrl+C no terminal de A tem o mesmo efeito.)
2. No Computador B: `./start.sh` / `start.bat`. Ele baixa a memória, assume a instância e avisa
   “🟢 TeleAgent online em notebook”.
3. No Telegram: `/projetos` → selecione o projeto. Se ele ainda não existir em B e a memória conhecer o
   repositório, aparece com ☁️ e o bot oferece **Clonar**.
4. `/continuar` — o agente lê a memória (objetivos, decisões, pendências, próximos passos, últimas tarefas),
   verifica o estado do código e segue de onde A parou.

### Se o Computador A ficou ligado/travado e você não tem acesso a ele

- Se A não der sinal por mais de `LEASE_STALE_HOURS` (padrão 3 h), B assume automaticamente.
- Para assumir antes: `./start.sh --takeover` (Windows: `start.bat --takeover`).
- Se A voltar a rodar depois, ele detecta (pelo heartbeat no Git ou pelo erro **409 Conflict** do Telegram)
  que outra máquina assumiu, avisa no Telegram e **se encerra sozinho**.

### Como a duplicidade é impedida

1. **Trava local do SO** (`data/teleagent.lock`, `fcntl`/`msvcrt`): duas instâncias na mesma máquina.
2. **Lease no repositório de memória** (`locks/instance.json`) com heartbeat a cada `HEARTBEAT_MINUTES`.
3. **Teste de polling** antes de iniciar (`getUpdates` → 409 = outra instância ativa) e tratamento do
   `Conflict` durante a execução.

### Conflitos de sincronização

- Alterações locais são **sempre commitadas antes** de buscar as remotas; usa-se `git merge`
  (nunca `reset`, `rebase` ou `push --force`).
- Conflito em `state.json`: resolvido automaticamente por **união por ID** (vence a edição mais recente do
  mesmo item); `MEMORY.md` é regenerado.
- Conflito em `locks/instance.json`: vence o heartbeat mais recente.
- Qualquer outro conflito (ex.: arquivo editado à mão nos dois lados): a mescla é **abortada**, seus commits
  locais ficam intactos, **nada é enviado** e você recebe a lista de arquivos para resolver em `data/sync/`.

---

## Segurança

**Autenticação**: todo update passa por verificação do `user.id` contra `TELEGRAM_OWNER_ID` e exige chat
privado. Outros usuários recebem “Acesso negado” e a tentativa é registrada no log; grupos são ignorados.
Botões de confirmação usam tokens aleatórios de uso único que expiram em 6 h.

**Diretórios autorizados**: projetos só podem ser selecionados dentro de `PROJECTS_DIRS` (nomes com `..`,
barras ou links simbólicos que apontam para fora são recusados). A pasta do próprio TeleAgent (código, `.env` com o token,
memória) nunca pode ser usada como projeto, mesmo que esteja dentro de uma pasta autorizada. O Claude Code roda com o projeto como
diretório de trabalho e `Edit/Write` restritos a `./**`; escrita fora do projeto é negada e **não** pode ser
aprovada pelo Telegram.

**Permissões do Claude Code** (sem desativar proteções — nunca usa `--dangerously-skip-permissions` nem
`bypassPermissions`):

- Modo `dontAsk`: tudo que não estiver na lista permitida é **negado** automaticamente.
- Lista permitida padrão: leitura/busca, edição dentro do projeto, `git status/diff/log/show/add/commit`,
  instalação e testes (`npm install`, `npm test`, `npm run`, `pytest`…), builds (`go build`, `cargo build`).
  `find` e `echo` ficam fora (permitiriam `-delete` e redirecionamento).
- Lista negada (sempre, mesmo que outra configuração permita): `rm`, `rmdir`, `unlink`, `shred`, `mv`,
  `chmod`, `chown`, `chgrp`, `sudo`, `su`, `dd`, `mkfs`, `git push`, `git reset --hard`, `git clean`,
  `git rebase`, `git restore`, `git checkout --`, `git branch -D`, `npm/pnpm/yarn publish`, `twine upload`,
  `cargo publish`, `docker push`, `gh release`, `gh pr merge`, `vercel`, `netlify deploy`, `firebase deploy`.
- Cada negação chega ao Telegram classificada (excluir arquivos, mover, permissões, publicar, reescrever
  histórico…). **Nunca são oferecidos para aprovação**: `sudo`, `su`, `mkfs`, `dd of=/dev/…`,
  desligar/reiniciar, `curl … | sh`, `rm` em `/`, `~`, `*`, ou exclusão/alteração de caminhos fora do projeto.
- Ao aprovar, apenas a regra exata (ex.: `Bash(rm build.log)`) é adicionada, e só as regras de negação que
  bloqueiam aquele comando são retiradas — **somente naquela continuação**.

> Por que não usar `acceptEdits`? Num teste real com a CLI 2.1.307, o modo `acceptEdits` aprovou
> automaticamente `rm` e `mv` dentro do diretório. O modo `dontAsk` + lista explícita não tem esse problema.

**Nenhum texto do Telegram vai para um shell**: tarefas viram prompt (stdin) do Claude Code; comandos de
instalação/Git são listas fixas de argumentos executadas sem shell.

**Segredos**: `.env` fica fora do Git (`.gitignore`) com permissão 600; o token do bot é removido do ambiente
passado ao Claude Code; logs e mensagens passam por redação (inclusive tracebacks); o log do `httpx` (que
contém o token na URL) fica em nível WARNING.

**Confirmação obrigatória** para: excluir/mover arquivos, alterar permissões, publicar (push, publish,
deploy), instalar dependências (`/instalar`), clonar projetos e desligar o bot.

### Ajuste fino

- Para permitir que o agente execute scripts do projeto sem confirmação (ex.: `python3 app.py`), acrescente
  regras em `CLAUDE_ALLOWED_TOOLS` — mas note que `python`/`node` arbitrários podem apagar arquivos sem passar
  pela lista negada. Prefira regras específicas (`Bash(python3 manage.py test:*)`).
- `npm run`, `pytest` etc. executam código do próprio projeto; remova-os da lista se não confiar no projeto.
- Configurações do Claude Code do usuário/projeto (`~/.claude/settings.json`, `.claude/settings.json`) também
  valem; regras `deny` delas continuam valendo, e `allow` amplas lá ampliam o que roda sem confirmação.

---

## Configurações (.env)

| Variável | Padrão | Descrição |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token do @BotFather |
| `TELEGRAM_OWNER_ID` | — | Seu ID numérico |
| `PROJECTS_DIRS` | — | Pastas de projetos, separadas por `;` |
| `SYNC_REPO_URL` | vazio | Repositório privado da memória (vazio = só local) |
| `SYNC_BRANCH` | `main` | Branch da memória |
| `MACHINE_NAME` | hostname | Nome desta máquina |
| `CLAUDE_BIN` | auto | Caminho do executável `claude` |
| `CLAUDE_MODEL` | padrão da CLI | `fable`, `opus`, `sonnet`, `haiku` ou nome completo |
| `CLAUDE_MAX_BUDGET_USD` | sem limite | `--max-budget-usd` por execução |
| `TASK_TIMEOUT_SECONDS` | `3600` | Tempo máximo por tarefa |
| `CLAUDE_ALLOWED_TOOLS` | lista segura | Substitui a lista permitida (separada por `;`) |
| `CLAUDE_EXTRA_DENIED_TOOLS` | vazio | Regras extras de negação |
| `LEASE_STALE_HOURS` | `3` | Horas sem heartbeat para outra máquina assumir |
| `HEARTBEAT_MINUTES` | `20` | Intervalo do heartbeat/sync periódico |
| `LOG_LEVEL` | `INFO` | Nível de log |

### Rodar como serviço (opcional)

Linux (systemd, usuário):
```ini
# ~/.config/systemd/user/teleagent.service
[Unit]
Description=TeleAgent
After=network-online.target
[Service]
WorkingDirectory=/caminho/para/teleagent
ExecStart=/caminho/para/teleagent/.venv/bin/python -m teleagent run
Restart=on-failure
RestartSec=30
[Install]
WantedBy=default.target
```
`systemctl --user enable --now teleagent`. Use `Restart=on-failure` (não `always`): o código de saída 3
indica recusa de início (outra máquina ativa) e não deve reiniciar em loop.

---

## Testes

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                         # testes automatizados (CLI do Claude simulada)
TELEAGENT_REAL_CLAUDE=1 .venv/bin/python -m pytest -q -s tests/test_integration_claude.py   # CLI real (custo pequeno, modelo haiku)
.venv/bin/python -m teleagent check                    # diagnóstico do ambiente
```

O que é coberto:

- `test_security.py` — redação de segredos, travessia de diretórios e symlinks, classificação de negações,
  liberação seletiva de regras.
- `test_memory_messaging.py` — divisão de mensagens, extração do bloco de memória, atualização de
  pendências/próximos passos, mescla entre máquinas.
- `test_gitsync_lock.py` — sincronização entre dois clones reais com remoto `bare`: conflito resolvido
  semanticamente, conflito não resolvível (nada sobrescrito, nada enviado), bloqueio de segredo, `.gitignore`
  do repositório de memória, trava local entre processos, regras da lease.
- `test_deps_runner.py` — detecção de gerenciadores, execução sem shell, flags da CLI, parsing de
  stream-json, cancelamento, timeout, falha da CLI, token do bot fora do ambiente do agente.
- `test_bot.py` — registro de comandos, rejeição de não-donos e grupos, fluxo completo de tarefa → memória →
  aprovação (retoma a mesma sessão só com o comando aprovado), recusa registrada, tarefa ocupada + `/parar`,
  fallback de sessão inexistente, recuperação de interrupção, resposta longa como arquivo, início com lease /
  409 / liberação no encerramento.
- `test_setup_wizard.py` — assistente gera `.env` válido (permissão 600), validação de configuração.
- `test_integration_claude.py` — **CLI real**: escrita permitida, `rm` negado e reportado, aprovação via
  `--resume` executa exatamente o comando aprovado.

---

## Solução de problemas

| Sintoma | Causa / solução |
|---|---|
| `Claude Code CLI não encontrado` | Instale o Claude Code ou defina `CLAUDE_BIN` |
| `/status` mostra “NÃO autenticado” | Rode `claude` no terminal da máquina e faça login |
| “O bot está ativo em 'X'” ao iniciar | Pare em X (`/parar` → Desligar) ou use `--takeover` se X estiver desligado |
| “409 Conflict” | Outra instância está rodando com o mesmo token (talvez fora do TeleAgent). Pare-a |
| “Falha no fetch/push” | Verifique rede e chave SSH (`ssh -T git@github.com`); a memória fica salva localmente |
| “Conflito que não pode ser resolvido” | Resolva em `data/sync/` com `git status`, faça commit e rode `/sync` |
| “Commit bloqueado: possível segredo” | Remova o segredo do arquivo indicado em `data/sync/memory/…` |
| Tarefa sem progresso | `/status` mostra a última ação; `/parar` cancela; logs completos em `logs/tasks/*.jsonl` |
| O agente pede aprovação para rodar o próprio código | Esperado (ver [Ajuste fino](#ajuste-fino)) |

---

## Limitações conhecidas

- **Testado no macOS** (Python 3.14, Claude Code 2.1.307). O código trata Windows (trava `msvcrt`,
  `taskkill /T`, `start.bat`, `claude.exe`), mas **não foi executado em Windows** nem em Linux nesta entrega;
  os testes que usam a CLI simulada são pulados no Windows.
- A conexão real com o Telegram depende do seu token: a lógica do bot foi testada com um cliente Telegram
  simulado e a inicialização real foi testada até a validação do token (um token falso é rejeitado com
  mensagem clara). Faça o primeiro teste real com `./start.sh` e `/start`.
- Sessões do Claude Code não migram entre computadores (por decisão de segurança); a continuidade
  entre máquinas é feita pela memória do projeto.
- Uma tarefa por vez. Mensagens de voz/arquivos não são processadas (somente texto).
- Regras de aprovação são passadas à CLI como argumentos; um comando aprovado que contenha vírgula pode ser
  interpretado de forma diferente pela CLI — nesse caso ele é negado de novo (falha segura).
