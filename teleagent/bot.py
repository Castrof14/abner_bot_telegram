"""Bot do Telegram: autenticação do proprietário, comandos e orquestração das tarefas."""
from __future__ import annotations

import asyncio
import functools
import html
import io
import json
import logging
import secrets as pysecrets
import time
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import deps
from .agents import (AGENT_NAME_RE, CLAUDE_MODELS, RUNNERS, Agent, AgentStore, is_free_model, opencode_models,
                     runner_label)
from .claude_runner import ClaudeRunner, Progress, RunResult
from .opencode_runner import OpenCodeRunner, OpenCodeResult, OpenCodeDenial
from .config import Config
from .gitsync import GitSync, git, project_git_info, project_safe_pull
from .instance_lock import Lease, LocalInstanceLock
from .memory import KINDS, MEMORY_INSTRUCTIONS, MemoryStore, extract_memory_block, render_context
from .messaging import DOCUMENT_THRESHOLD, split_message, truncate
from .security import (PROJECT_NAME_RE, SENSITIVE_FILENAMES, PathError, contains_secret, list_projects,
                       redact, resolve_project, review_denial)
from .storage import LocalState, now_iso

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Apresentação e estado rápido"),
    ("help", "Lista de comandos"),
    ("status", "Estado da máquina, projeto, tarefa e sincronização"),
    ("projetos", "Lista e seleciona projetos"),
    ("criar", "Cria uma pasta de projeto nova e a seleciona"),
    ("memoria", "Mostra/edita a memória do projeto"),
    ("continuar", "Retoma o projeto a partir da memória"),
    ("parar", "Cancela a tarefa atual ou desliga o bot nesta máquina"),
    ("instalar", "Detecta e instala dependências do projeto"),
    ("sync", "Sincroniza a memória com o repositório Git"),
    ("publicar", "Commit + push do projeto (com confirmação)"),
    ("novo", "Inicia uma sessão nova do Claude Code no projeto"),
    ("agentes", "Lista, cria e seleciona agentes de IA (Claude ou OpenCode)"),
    ("agente", "Cria/edita um agente: /agente novo NOME"),
    ("logs", "Últimas linhas do log"),
]

HELP_TEXT = """\
<b>TeleAgent</b> — Claude Code pelo Telegram

<b>Comandos</b>
/projetos — lista projetos e permite selecionar (ou <code>/projetos nome</code>)
/criar nome — cria uma pasta de projeto nova (você escolhe onde) e a seleciona
/status — máquina, projeto, tarefa em andamento e sincronização
/memoria — mostra a memória do projeto
  <code>/memoria objetivo|decisao|pendencia|proximo|nota TEXTO</code> — adiciona item
/continuar [instruções] — retoma de onde parou (inclusive em outro computador)
/parar — cancela a tarefa; sem tarefa, oferece desligar o bot nesta máquina
/instalar — detecta npm/pnpm/yarn/bun/pip/uv/poetry/… e instala (com confirmação)
/sync — sincroniza a memória agora
/publicar [mensagem] — commit e push do projeto (com confirmação)
/novo — esquece a sessão do Claude Code deste projeto nesta máquina
/agentes — lista os agentes de IA e escolhe qual usar no projeto
/agente novo NOME — cria um agente (motor Claude ou OpenCode, modelo e instruções)
  <code>/agente NOME</code> — ver/usar/editar · <code>/agente usar|modelo|instrucoes|apagar NOME</code>
/logs — final do log

<b>Tarefas</b>: com um projeto selecionado, envie o pedido em texto normal.
Ações sensíveis (excluir, mover, permissões, push/publicar, comandos fora da lista segura)
são negadas pelo Claude Code e chegam aqui para você aprovar ou recusar."""

PROMPT_TEMPLATE = """\
Você está trabalhando no projeto "{name}" (diretório atual: {path}), a pedido do proprietário via \
Telegram (TeleAgent, máquina "{machine}").

Regras desta sessão:
- Trabalhe dentro deste diretório. Se a tarefa pedir algo fora dele (criar ou mover pastas em outro lugar), \
tente a ação diretamente com o caminho completo: ela será negada e o proprietário poderá aprová-la pelo Telegram.
- Para criar ou alterar arquivos (dentro ou fora do projeto) use sempre as ferramentas Write/Edit, nunca \
Bash com echo, cat, heredoc ou redirecionamento. Comandos Bash devem ter uma linha só e fazer uma coisa só: \
comandos multilinha ou encadeados não podem ser aprovados pelo Telegram.
- Ações não pré-aprovadas (excluir ou mover arquivos, alterar permissões, push/publicação, comandos \
fora da lista segura) são negadas automaticamente. NÃO tente contorná-las com outros comandos: \
termine o que for possível e diga claramente o que precisa de aprovação.
- Nunca exiba nem grave segredos (tokens, senhas, chaves).
- Responda em português, de forma objetiva: a resposta será lida no Telegram.
{agent_section}
Memória persistente do projeto (pode vir de outro computador):
{context}

{task_header}
{task}

{memory_instructions}"""


AGENT_USAGE = """\
Agentes de IA
/agentes — lista e seleciona
/agente novo NOME — cria (escolhe motor, modelo e instruções)
/agente NOME — mostra o agente com botões
/agente usar NOME — usa no projeto selecionado (ou como padrão)
/agente nenhum — volta ao runner padrão
/agente modelo NOME — troca motor/modelo
/agente instrucoes NOME [TEXTO] — define as instruções
/agente apagar NOME"""

AWAIT_TTL = 15 * 60


class StartupError(Exception):
    pass


def owner_only(func):
    """Rejeita qualquer usuário que não seja o proprietário (ID numérico) e chats não privados."""
    @functools.wraps(func)
    async def wrapper(self: "TeleAgentBot", update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        chat = update.effective_chat
        if not user or user.id != self.cfg.owner_id or not chat or chat.type != ChatType.PRIVATE:
            log.warning("Acesso negado: user_id=%s username=%s chat=%s",
                        getattr(user, "id", None), getattr(user, "username", None), getattr(chat, "id", None))
            if update.callback_query:
                try:
                    await update.callback_query.answer("Acesso negado.", show_alert=True)
                except TelegramError:
                    pass
            elif update.effective_message and chat and chat.type == ChatType.PRIVATE:
                try:
                    await update.effective_message.reply_text("⛔ Acesso negado.")
                except TelegramError:
                    pass
            return None
        return await func(self, update, context)
    return wrapper


class TeleAgentBot:
    def __init__(self, cfg: Config, takeover: bool = False):
        self.cfg = cfg
        self.takeover = takeover
        self.secrets = [cfg.telegram_token]
        self.state = LocalState(cfg.local_state_file)
        self.memory = MemoryStore(cfg.sync_dir, cfg.machine_name, self.secrets)
        self.sync = GitSync(cfg.sync_dir, cfg.sync_repo_url, cfg.sync_branch, cfg.machine_name)
        self.lease = Lease(cfg.sync_dir, cfg.machine_name, cfg.lease_stale_hours)
        self.local_lock = LocalInstanceLock(cfg.data_dir / "teleagent.lock")
        self.runner = ClaudeRunner(cfg.claude_bin, cfg.allowed_tools, cfg.denied_tools, cfg.claude_model,
                                   cfg.claude_max_budget_usd, cfg.task_timeout, cfg.log_dir / "tasks")
        self.opencode = OpenCodeRunner(cfg.opencode_bin, cfg.opencode_model,
                                       cfg.task_timeout, cfg.log_dir / "tasks")
        # Runner ativo por projeto: {"projeto": "claude"|"opencode"}
        self._project_runner: dict[str, str] = {}
        self._default_runner: str = cfg.default_runner  # "claude" ou "opencode"
        self.agents = AgentStore(cfg.data_dir / "agents.json")
        # Próxima mensagem de texto esperada por um assistente (ex.: instruções de um agente).
        self._awaiting: dict | None = None
        # O próprio TeleAgent (código, .env com o token, memória) nunca é um projeto.
        self.excluded = [cfg.base_dir.resolve()]
        self.app: Application | None = None
        self.tg = None  # telegram.Bot (substituível em testes)
        self.task: asyncio.Task | None = None
        self.task_meta: dict = {}
        self.pending: dict[str, dict] = {}
        self._project_cache: list[str] = []
        self._heartbeat_task: asyncio.Task | None = None
        self._stopping = False
        self._lock_held = False

    # ------------------------------------------------------------ montagem

    def build(self) -> Application:
        app = (ApplicationBuilder().token(self.cfg.telegram_token)
               .post_init(self._post_init).post_shutdown(self._post_shutdown)
               .concurrent_updates(True).build())
        handlers = {
            "start": self.cmd_start, "help": self.cmd_help, "status": self.cmd_status,
            "projetos": self.cmd_projetos, "projeto": self.cmd_projetos, "criar": self.cmd_criar, "memoria": self.cmd_memoria,
            "continuar": self.cmd_continuar, "parar": self.cmd_parar, "instalar": self.cmd_instalar,
            "sync": self.cmd_sync, "publicar": self.cmd_publicar, "novo": self.cmd_novo, "logs": self.cmd_logs,
            "runner": self.cmd_runner, "agentes": self.cmd_agentes, "agente": self.cmd_agente,
        }
        for name, fn in handlers.items():
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.COMMAND, self.on_unknown_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND & ~filters.StatusUpdate.ALL, self.on_other))
        app.add_error_handler(self.on_error)
        self.app = app
        self.tg = app.bot
        return app

    # ------------------------------------------------------------ ciclo de vida

    async def _post_init(self, app: Application) -> None:
        try:
            self.local_lock.acquire()
        except Exception as e:
            raise StartupError(str(e))
        self._lock_held = True
        rep = await self.sync.ensure_repo()
        if not rep.ok:
            raise StartupError(rep.message)
        rep = await self.sync.sync("TeleAgent: início")
        if not rep.ok:
            log.warning("Sincronização inicial falhou: %s", rep.message)
        check = self.lease.check(self.takeover)
        if not check.can_start:
            raise StartupError(check.message)
        log.info(check.message)
        self.lease.write()
        rep2 = await self.sync.sync("TeleAgent: assume instância")
        if not rep2.ok:
            log.warning("Não foi possível publicar a lease: %s", rep2.message)

        # Barreira final: se outra instância estiver em long polling, o Telegram responde 409.
        try:
            await app.bot.get_updates(offset=-1, limit=1, timeout=0)
        except Conflict:
            self.lease.write(released=True)
            await self.sync.sync("TeleAgent: libera instância (conflito)")
            raise StartupError("O Telegram reportou outra instância fazendo polling deste bot (409 Conflict). "
                               "Pare a outra instância antes de iniciar esta.")
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        await self._recover_interrupted()
        sync_msg = rep.message if rep.ok else f"⚠️ {rep.message}"
        await self.notify(f"🟢 TeleAgent online em <b>{html.escape(self.cfg.machine_name)}</b>.\n"
                          f"Projeto: <code>{html.escape(self.state.selected_project or '—')}</code>\n"
                          f"Memória: {html.escape(sync_msg)}\n{html.escape(check.message)}", parse_html=True)

    async def _post_shutdown(self, app: Application) -> None:
        await self.cleanup()

    async def cleanup(self) -> None:
        if self._stopping or not self._lock_held:
            return
        self._stopping = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.task and not self.task.done():
            await self.runner.cancel()
            try:
                await asyncio.wait_for(self.task, 30)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await self.sync.sync("TeleAgent: encerramento")
            cur = self.lease.read()
            if cur and cur.get("instance") == self.lease.instance_id:
                self.lease.write(released=True)
                rep = await self.sync.sync("TeleAgent: libera instância")
                log.info("Lease liberada: %s", rep.message)
        except Exception:  # noqa: BLE001
            log.exception("Erro ao liberar lease")
        self.local_lock.release()

    async def _heartbeat_loop(self) -> None:
        interval = max(60.0, self.cfg.heartbeat_minutes * 60)
        while True:
            await asyncio.sleep(interval)
            try:
                rep = await self.sync.sync("TeleAgent: heartbeat")
                other = self.lease.owned_by_other()
                if other:
                    await self.notify(f"⚠️ A máquina '{other.get('machine')}' assumiu o bot. "
                                      f"Encerrando a instância em '{self.cfg.machine_name}'.")
                    self.app.stop_running()
                    return
                self.lease.write()
                if rep.ok:
                    await self.sync.sync("TeleAgent: heartbeat")
                if rep.changed_projects:
                    log.info("Memória atualizada remotamente: %s", rep.changed_projects)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Falha no heartbeat")

    async def _recover_interrupted(self) -> None:
        info = self.state.running_task
        if not info:
            return
        project = info.get("project")
        state = self.memory.load(project)
        self.memory.finish_task(state, info.get("task_id"), "interrupted",
                                "Interrompida por reinício/queda do TeleAgent.", None)
        self.memory.save(project, state)
        self.state.running_task = None
        await self.sync.sync(f"TeleAgent: registra interrupção em {project}")
        await self.notify(f"⚠️ A tarefa em <code>{html.escape(project)}</code> foi interrompida "
                          f"(início {html.escape(info.get('started', ''))}):\n"
                          f"<i>{html.escape(truncate(info.get('prompt', ''), 300))}</i>\n"
                          "Use /continuar para retomá-la.", parse_html=True)

    # ------------------------------------------------------------ envio

    async def _call(self, fn, *args, **kwargs):
        for attempt in range(4):
            try:
                return await fn(*args, **kwargs)
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 0.5)
            except NetworkError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def notify(self, text: str, parse_html: bool = False, markup=None) -> object:
        text = redact(text, self.secrets)
        chunks = [text] if parse_html else split_message(text)
        msg = None
        for i, chunk in enumerate(chunks):
            kwargs = {"reply_markup": markup} if (markup and i == len(chunks) - 1) else {}
            if parse_html:
                kwargs["parse_mode"] = "HTML"
            try:
                msg = await self._call(self.tg.send_message, self.cfg.owner_id, chunk, **kwargs)
            except BadRequest:
                if parse_html:  # HTML malformado: envia como texto puro
                    msg = await self._call(self.tg.send_message, self.cfg.owner_id, chunk,
                                           **({"reply_markup": markup} if markup else {}))
                else:
                    raise
            except TelegramError:
                log.exception("Falha ao enviar mensagem")
        return msg

    async def send_long(self, text: str, filename: str = "resposta.md") -> None:
        text = redact(text, self.secrets)
        if len(text) > DOCUMENT_THRESHOLD:
            await self.notify(truncate(text, 1500) + "\n\n📎 Resposta completa no arquivo anexo.")
            try:
                await self._call(self.tg.send_document, self.cfg.owner_id,
                                 document=io.BytesIO(text.encode("utf-8")), filename=filename)
            except TelegramError:
                log.exception("Falha ao enviar documento; enviando em partes")
                await self.notify(text)
        else:
            await self.notify(text)

    async def edit(self, msg, text: str) -> None:
        if msg is None:
            return
        try:
            await self.tg.edit_message_text(redact(text, self.secrets)[:4000], chat_id=self.cfg.owner_id,
                                            message_id=msg.message_id)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.debug("edit falhou: %s", e)
        except RetryAfter:
            pass
        except TelegramError as e:
            log.debug("edit falhou: %s", e)

    def _token(self, data: dict) -> str:
        # Limpa pedidos de confirmação antigos (expiram em 6 h).
        cutoff = time.time() - 6 * 3600
        for k in [k for k, v in self.pending.items() if v.get("ts", 0) < cutoff]:
            self.pending.pop(k, None)
        tok = pysecrets.token_urlsafe(8)
        self.pending[tok] = {**data, "ts": time.time()}
        return tok

    # ------------------------------------------------------------ projeto atual

    def current_project(self) -> tuple[str, Path] | None:
        name = self.state.selected_project
        if not name:
            return None
        try:
            return name, resolve_project(name, self.cfg.projects_dirs, self.excluded)
        except PathError:
            return None

    async def _require_project(self, update: Update) -> tuple[str, Path] | None:
        cur = self.current_project()
        if not cur:
            await update.effective_message.reply_text("Nenhum projeto selecionado (ou ele não existe nesta máquina). "
                                                      "Use /projetos.")
        return cur

    # ------------------------------------------------------------ comandos

    @owner_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = self.state.selected_project or "nenhum"
        await update.effective_message.reply_html(
            f"👋 TeleAgent ativo em <b>{html.escape(self.cfg.machine_name)}</b>.\n"
            f"Projeto selecionado: <code>{html.escape(cur)}</code>\n\n"
            "Use /projetos para escolher um projeto e depois envie tarefas em linguagem natural. /help para ajuda.")

    @owner_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_html(HELP_TEXT)

    @owner_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = self.current_project()
        lines = [f"🖥️ Máquina: {self.cfg.machine_name}"]
        runner, model, agent = self._active_profile(cur[0] if cur else None)
        lines.append(f"⚙️ Runner ativo: {runner_label(runner)} / modelo {model or 'padrão'} "
                     f"— agente: {agent.name if agent else 'nenhum'} (mude com /agentes)")
        lines.append(f"🤖 Claude Code: {await self.runner.version()}")
        auth = await self.runner.auth_status()
        if auth is not None:
            lines.append(f"🔐 Login Claude: {'ok' if auth.get('loggedIn') else 'NÃO autenticado — rode `claude` e faça login'}")
        oc_ver = await self.opencode.version() if self.opencode.opencode_bin else "não instalado"
        lines.append(f"🔀 opencode: {oc_ver}")
        if cur:
            name, path = cur
            lines.append(f"📁 Projeto: {name} ({path})")
            info = await project_git_info(path)
            if info.get("is_repo"):
                lines.append(f"   git: {info.get('branch')} | {'alterações locais' if info.get('dirty') else 'limpo'}"
                             f" | upstream: {info.get('upstream') or '—'}")
            st = self.memory.load(name)
            lines.append(f"   memória: {len(self.memory.open_items(st, 'pending'))} pendência(s), "
                         f"{len(self.memory.open_items(st, 'next'))} próximo(s) passo(s)")
            lines.append(f"   sessão local: {self.state.session_for(name) or '—'}")
        else:
            lines.append("📁 Projeto: nenhum")
        if self.task and not self.task.done():
            p = self.runner.progress
            elapsed = int(time.monotonic() - p.started) if p else 0
            lines.append(f"⏳ Tarefa: {self.task_meta.get('project')} há {elapsed // 60}m{elapsed % 60:02d}s — "
                         f"{p.tool_calls if p else 0} ações — {p.last_action if p else ''}")
        else:
            lines.append("✅ Nenhuma tarefa em andamento")
        lines.append(f"🔄 Sync: {self.cfg.sync_repo_url or 'sem remoto'} — {await self.sync.status_line()}")
        if self.sync.last_report:
            lines.append(f"   último: {'ok' if self.sync.last_report.ok else 'ERRO'} — {self.sync.last_report.message}")
        lease = self.lease.read() or {}
        lines.append(f"🔒 Instância: {lease.get('machine', '?')} (heartbeat {lease.get('heartbeat', '?')})")
        await update.effective_message.reply_text("\n".join(lines))

    @owner_only
    async def cmd_projetos(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.args:
            await self._select_project(" ".join(context.args).strip())
            return
        local = list_projects(self.cfg.projects_dirs, self.excluded)
        local_names = {n for n, _ in local}
        remote_only = [p for p in self.memory.known_projects() if p not in local_names]
        names = [n for n, _ in local] + remote_only
        self._project_cache = names
        if not names:
            await update.effective_message.reply_text(
                "Nenhum projeto encontrado. Cada projeto é uma subpasta de: "
                + ", ".join(map(str, self.cfg.projects_dirs))
                + "\nCrie ou copie a pasta do projeto lá dentro e envie /projetos de novo.")
            return
        sel = self.state.selected_project
        buttons = []
        for i, n in enumerate(names[:60]):
            mark = "✅ " if n == sel else ("☁️ " if n in remote_only else "")
            buttons.append(InlineKeyboardButton(truncate(mark + n, 40), callback_data=f"sel:{i}"))
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        legend = "\n☁️ = existe só na memória (clone disponível se o repositório for conhecido)" if remote_only else ""
        await update.effective_message.reply_text(f"Projetos ({len(names)}). Toque para selecionar:{legend}",
                                                  reply_markup=InlineKeyboardMarkup(rows))

    async def _select_project(self, name: str) -> None:
        try:
            path = resolve_project(name, self.cfg.projects_dirs, self.excluded)
        except PathError as e:
            st = self.memory.load(name) if name in self.memory.known_projects() else None
            if st and st.get("repo_url") and PROJECT_NAME_RE.match(name):
                tok = self._token({"action": "clone", "name": name, "url": st["repo_url"]})
                await self.notify(f"O projeto '{name}' existe na memória, mas não nesta máquina.\n"
                                  f"Clonar {st['repo_url']} em {self.cfg.projects_dirs[0] / name}?",
                                  markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("📥 Clonar", callback_data=f"ok:{tok}"),
                                      InlineKeyboardButton("Cancelar", callback_data=f"no:{tok}")]]))
            else:
                await self.notify(f"❌ {e}")
            return
        self.state.selected_project = name
        st = self.memory.load(name)
        info = await project_git_info(path)
        if info.get("remote") and not st.get("repo_url"):
            self.memory.set_repo_url(st, info["remote"])
            self.memory.save(name, st)
        lines = [f"📁 Projeto selecionado: {name}", f"   {path}"]
        if info.get("is_repo"):
            lines.append(f"   git: {info.get('branch')} ({'com alterações locais' if info.get('dirty') else 'limpo'})")
        pend, nxt = self.memory.open_items(st, "pending"), self.memory.open_items(st, "next")
        if pend or nxt:
            lines.append(f"   memória: {len(pend)} pendência(s), {len(nxt)} próximo(s) passo(s) — /continuar para retomar")
        if deps.looks_uninstalled(path):
            lines.append("   ⚠️ dependências parecem não instaladas — use /instalar")
        lines.append("\nEnvie a tarefa em texto.")
        await self.notify("\n".join(lines))

    @owner_only
    async def cmd_criar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        name = " ".join(context.args).strip()
        if not name:
            await update.effective_message.reply_text("Uso: /criar nome-da-pasta")
            return
        if not PROJECT_NAME_RE.match(name) or ".." in name:
            await update.effective_message.reply_text(
                "Nome inválido. Use letras, números, espaço, ponto, hífen ou _ (sem barras).")
            return
        roots = self.cfg.projects_dirs
        if len(roots) == 1:
            await self._create_project(roots[0], name)
            return
        tok = self._token({"action": "create", "name": name})
        buttons = [[InlineKeyboardButton(f"📁 {truncate(str(r), 50)}", callback_data=f"mk:{tok}:{i}")]
                   for i, r in enumerate(roots)]
        await update.effective_message.reply_text(f"Onde criar a pasta '{name}'?",
                                                  reply_markup=InlineKeyboardMarkup(buttons))

    async def _create_project(self, root: Path, name: str) -> None:
        dest = root / name
        if dest.exists():
            await self.notify(f"A pasta {dest} já existe; selecionando.")
        else:
            dest.mkdir()
            await self.notify(f"✅ Pasta criada: {dest}")
        try:
            if resolve_project(name, self.cfg.projects_dirs, self.excluded) != dest.resolve():
                await self.notify(f"⚠️ Já existe outro projeto chamado '{name}' numa pasta anterior da lista; "
                                  "ele tem prioridade. Use um nome diferente para selecionar a pasta nova.")
                return
        except PathError:
            pass
        await self._select_project(name)

    @owner_only
    async def cmd_memoria(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        name = self.state.selected_project
        if not name:
            await update.effective_message.reply_text("Selecione um projeto com /projetos.")
            return
        aliases = {"objetivo": "objective", "decisao": "decision", "decisão": "decision", "pendencia": "pending",
                   "pendência": "pending", "proximo": "next", "próximo": "next", "nota": "note", "feito": "done"}
        if context.args:
            kind = aliases.get(context.args[0].lower())
            text = " ".join(context.args[1:]).strip()
            if not kind or not text:
                await update.effective_message.reply_text("Uso: /memoria objetivo|decisao|pendencia|proximo|nota|feito TEXTO")
                return
            st = self.memory.load(name)
            self.memory.add_item(st, kind, text)
            self.memory.save(name, st)
            rep = await self.sync.sync(f"TeleAgent: memória manual em {name}")
            await update.effective_message.reply_text(f"✅ Adicionado em {KINDS[kind]}. {rep.message}")
            return
        path = self.memory.markdown_path(name)
        if not path.exists():
            await update.effective_message.reply_text("Ainda não há memória para este projeto.")
            return
        await self.send_long(path.read_text(encoding="utf-8"), filename=f"MEMORY-{name}.md")

    @owner_only
    async def cmd_continuar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._busy(update):
            return
        cur = await self._require_project(update)
        if not cur:
            return
        name, path = cur
        msg = await update.effective_message.reply_text("🔄 Sincronizando memória e projeto…")
        rep = await self.sync.sync("TeleAgent: antes de continuar")
        pull = await project_safe_pull(path)
        st = self.memory.load(name)
        ctx = render_context(st)
        notes = [f"Memória: {rep.message}", f"Projeto: {pull}"]
        if deps.looks_uninstalled(path):
            notes.append("⚠️ Dependências parecem não instaladas (/instalar).")
        await self.edit(msg, "\n".join(notes))
        if ctx.startswith("(sem memória"):
            await self.notify("Não há memória registrada para continuar. Envie uma tarefa em texto.")
            return
        extra = " ".join(context.args).strip()
        task = ("Retome o trabalho deste projeto a partir da memória acima. Comece verificando o estado atual "
                "do código (git status/diff, arquivos relevantes), depois avance nas pendências e próximos passos "
                "em ordem de prioridade.")
        if extra:
            task += f"\nInstruções adicionais do proprietário: {extra}"
        self._start_task(name, path, task, kind="continue", display=f"/continuar {extra}".strip())

    @owner_only
    async def cmd_parar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.task and not self.task.done():
            await update.effective_message.reply_text("🛑 Cancelando a tarefa…")
            if not await self.runner.cancel():
                self.task.cancel()  # instalação de dependências
            return
        tok = self._token({"action": "shutdown"})
        await update.effective_message.reply_text(
            "Nenhuma tarefa em andamento.\nDeseja DESLIGAR o TeleAgent nesta máquina e liberar o bot para outro "
            "computador? (a memória será sincronizada antes)",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏻ Desligar aqui", callback_data=f"ok:{tok}"),
                InlineKeyboardButton("Cancelar", callback_data=f"no:{tok}")]]))

    @owner_only
    async def cmd_instalar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._busy(update):
            return
        cur = await self._require_project(update)
        if not cur:
            return
        name, path = cur
        steps = deps.detect(path)
        if not steps:
            await update.effective_message.reply_text("Nenhum gerenciador de dependências reconhecido neste projeto.")
            return
        lines = [f"📦 Dependências detectadas em {name}:"]
        for s in steps:
            lines.append(f"{'•' if s.available else '✖'} {s.display}   ({s.reason}"
                         f"{'' if s.available else ' — NÃO instalado nesta máquina'})")
        lines.append("\nScripts de instalação dos pacotes podem executar código. Confirmar?")
        tok = self._token({"action": "install", "project": name})
        await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Instalar", callback_data=f"ok:{tok}"),
            InlineKeyboardButton("Cancelar", callback_data=f"no:{tok}")]]))

    @owner_only
    async def cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = await update.effective_message.reply_text("🔄 Sincronizando…")
        rep = await self.sync.sync("TeleAgent: sync manual")
        extra = f"\nProjetos atualizados: {', '.join(rep.changed_projects)}" if rep.changed_projects else ""
        await self.edit(msg, f"{'✅' if rep.ok else '⚠️'} {rep.message}{extra}")

    @owner_only
    async def cmd_publicar(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if await self._busy(update):
            return
        cur = await self._require_project(update)
        if not cur:
            return
        name, path = cur
        info = await project_git_info(path)
        if not info.get("is_repo"):
            await update.effective_message.reply_text("Este projeto não é um repositório Git.")
            return
        if not info.get("remote"):
            await update.effective_message.reply_text("O projeto não tem remoto 'origin' configurado.")
            return
        changes = info.get("changes") or []
        sensitive = [c for c in changes if SENSITIVE_FILENAMES.search(c[3:].strip().strip('"'))]
        message = " ".join(context.args).strip() or f"TeleAgent: atualizações em {name}"
        lines = [f"🚀 Publicar {name}", f"branch: {info.get('branch')} → {info.get('upstream') or 'origin/' + info.get('branch', '')}",
                 f"remoto: {info.get('remote')}", f"{len(changes)} arquivo(s) alterado(s):"]
        lines += [f"  {c}" for c in changes[:25]] + (["  …"] if len(changes) > 25 else [])
        if sensitive:
            lines.append("\n⛔ Arquivos sensíveis entre as alterações (adicione ao .gitignore antes):")
            lines += [f"  {c}" for c in sensitive]
            await update.effective_message.reply_text("\n".join(lines))
            return
        lines.append(f"\nMensagem do commit: {message}\nIsso fará commit de TODAS as alterações e push (sem --force).")
        tok = self._token({"action": "publish", "project": name, "message": message})
        await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Publicar", callback_data=f"ok:{tok}"),
            InlineKeyboardButton("Cancelar", callback_data=f"no:{tok}")]]))

    @owner_only
    async def cmd_novo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        cur = await self._require_project(update)
        if cur:
            self.state.set_session(cur[0], None)
            await update.effective_message.reply_text("🆕 A próxima tarefa iniciará uma sessão nova do Claude Code "
                                                      "(a memória do projeto continua sendo usada).")

    @owner_only
    async def cmd_runner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Troca o runner do projeto atual: /runner claude | /runner opencode [modelo]"""
        cur = self.current_project()
        args = (context.args or [])
        if not args:
            name = cur[0] if cur else "(nenhum)"
            active = self._active_runner_name(cur[0] if cur else None)
            oc_ver = await self.opencode.version() if self.opencode.opencode_bin else "não instalado"
            await update.effective_message.reply_text(
                f"Runner atual do projeto <b>{name}</b>: <b>{active}</b>\n\n"
                f"Claude Code: {await self.runner.version()}\n"
                f"opencode: {oc_ver}\n\n"
                "Para trocar:\n"
                "  /runner claude\n"
                "  /runner opencode\n"
                "  /runner opencode opencode/cat-jam-free\n"
                "  /runner opencode anthropic/claude-sonnet-4-5",
                parse_mode="HTML",
            )
            return
        choice = args[0].lower()
        if choice not in ("claude", "opencode"):
            await update.effective_message.reply_text("Use: /runner claude  ou  /runner opencode [modelo]")
            return
        if choice == "opencode" and not self.opencode.opencode_bin:
            await update.effective_message.reply_text("⚠️ opencode CLI não encontrado. Instale com:\n  npm i -g opencode-ai")
            return
        model_override = args[1] if len(args) > 1 else ""
        self.state.set_agent(cur[0] if cur else None, None)
        if cur:
            self._project_runner[cur[0]] = choice
            if model_override:
                self._project_runner[cur[0] + ":model"] = model_override
            elif cur[0] + ":model" in self._project_runner:
                del self._project_runner[cur[0] + ":model"]
        else:
            self._default_runner = choice
        name = cur[0] if cur else "(global)"
        model_info = f" com modelo {model_override}" if model_override else ""
        await update.effective_message.reply_text(
            f"✅ Runner do projeto <b>{name}</b> definido para <b>{choice}</b>{model_info}.\n"
            f"A próxima tarefa já usará o novo runner.",
            parse_mode="HTML",
        )

    def _active_runner_name(self, project: str | None) -> str:
        if project and project in self._project_runner:
            return self._project_runner[project]
        return self._default_runner

    def _active_profile(self, project: str | None) -> tuple[str, str, Agent | None]:
        """(motor, modelo, agente) usados nas tarefas do projeto. Agente do projeto > /runner do projeto >
        agente padrão > runner padrão."""
        inherit = not (project and project in self._project_runner)
        agent = self.agents.get(self.state.agent_for(project, inherit=inherit))
        if agent:
            return agent.runner, agent.model, agent
        runner = self._active_runner_name(project)
        if runner == "opencode":
            return runner, (project and self._project_runner.get(project + ":model")) or self.cfg.opencode_model, None
        return runner, self.cfg.claude_model, None

    # ------------------------------------------------------------ agentes

    @owner_only
    async def cmd_agentes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._show_agents()

    @owner_only
    async def cmd_agente(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args or []
        reply = update.effective_message.reply_text
        if not args:
            await reply(AGENT_USAGE)
            return
        sub, name = args[0].lower(), (args[1] if len(args) > 1 else "")
        if sub in ("novo", "criar"):
            if not AGENT_NAME_RE.match(name):
                await reply("Uso: /agente novo NOME (1 a 32 letras, números, - ou _). Ex.: /agente novo revisor")
            elif self.agents.get(name):
                await reply(f"Já existe o agente '{name}'. Use /agente {name} para editá-lo.")
            else:
                await self._ask_runner(name, new=True)
            return
        if sub in ("lista", "listar"):
            await self._show_agents()
            return
        if sub == "nenhum":
            await self._use_agent(None)
            return
        if sub not in ("usar", "ver", "modelo", "instrucoes", "instruções", "apagar"):
            name, sub = args[0], "ver"
        agent = self.agents.get(name)
        if not agent:
            await reply(f"Agente '{name}' não encontrado. Veja /agentes ou crie com /agente novo NOME.")
            return
        if sub == "usar":
            await self._use_agent(agent.name)
        elif sub == "ver":
            await self._show_agent(agent)
        elif sub == "modelo":
            await self._ask_runner(agent.name, new=False)
        elif sub in ("instrucoes", "instruções"):
            text = " ".join(args[2:]).strip()
            if text:
                await self._save_instructions(agent.name, text)
            else:
                await self._ask_instructions(agent.name, new=False)
        elif sub == "apagar":
            await self._confirm_delete_agent(agent.name)

    async def _show_agents(self) -> None:
        project = self.state.selected_project
        current = self._active_profile(project)[2]
        agents = self.agents.list()
        if not agents:
            await self.notify("Nenhum agente criado ainda.\nCrie um com /agente novo NOME (ex.: /agente novo revisor): "
                              "você escolhe o motor (Claude Code ou OpenCode com modelos grátis), o modelo e as "
                              "instruções.")
            return
        where = f"no projeto {project}" if project else "como padrão"
        lines = [f"🤖 Agentes ({len(agents)}) — em uso {where}: {current.name if current else 'nenhum'}"]
        for a in agents:
            free = " 🆓" if is_free_model(a.model) else ""
            lines.append(f"{'✅' if current and a.name == current.name else '•'} {a.name} — "
                         f"{runner_label(a.runner)} / {a.model or 'padrão'}{free}")
        lines.append("\nToque num agente para ver, usar ou editar. Novo: /agente novo NOME")
        buttons = [InlineKeyboardButton(("✅ " if current and a.name == current.name else "") + a.name,
                                        callback_data=f"ag:show:{a.name}") for a in agents[:40]]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        if current:
            rows.append([InlineKeyboardButton("🚫 Não usar agente", callback_data="ag:none:")])
        await self.notify("\n".join(lines), markup=InlineKeyboardMarkup(rows))

    async def _show_agent(self, agent: Agent) -> None:
        project = self.state.selected_project
        current = self._active_profile(project)[2]
        text = agent.describe()
        if current and current.name == agent.name:
            text += f"\n\n✅ Em uso {'no projeto ' + project if project else 'como padrão'}"
        use = f"✅ Usar em {truncate(project, 20)}" if project else "✅ Usar como padrão"
        n = agent.name
        await self.notify(text, markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(use, callback_data=f"ag:use:{n}"),
             InlineKeyboardButton("🔀 Motor/modelo", callback_data=f"ag:model:{n}")],
            [InlineKeyboardButton("📝 Instruções", callback_data=f"ag:instr:{n}"),
             InlineKeyboardButton("🗑️ Apagar", callback_data=f"ag:del:{n}")]]))

    async def _use_agent(self, name: str | None) -> None:
        project = self.state.selected_project
        self.state.set_agent(project, name)
        if project:
            self._project_runner.pop(project, None)
            self._project_runner.pop(project + ":model", None)
        runner, model, agent = self._active_profile(project)
        where = f"no projeto {project}" if project else "como padrão (projetos sem agente próprio)"
        who = f"agente {agent.name}" if agent else "nenhum agente"
        lines = [f"✅ Em uso {where}: {who}", f"   motor: {runner_label(runner)} / modelo: {model or 'padrão'}",
                 "A próxima tarefa já usa essa configuração."]
        if runner == "opencode" and not self.opencode.opencode_bin:
            lines.append("⚠️ opencode CLI não encontrado nesta máquina. Instale com: npm i -g opencode-ai")
        await self.notify("\n".join(lines))

    async def _ask_runner(self, name: str, new: bool) -> None:
        tok = self._token({"action": "agent_runner", "name": name, "new": new})
        await self.notify(f"{'🆕 Novo agente' if new else '🔀 Agente'} {name}: qual motor de IA?\n"
                          "• Claude Code — usa sua conta Claude (Sonnet, Opus, Haiku)\n"
                          "• OpenCode — modelos gratuitos do OpenCode Zen (e outros provedores configurados)",
                          markup=InlineKeyboardMarkup([[
                              InlineKeyboardButton("🟣 Claude Code", callback_data=f"agr:{tok}:claude"),
                              InlineKeyboardButton("🆓 OpenCode", callback_data=f"agr:{tok}:opencode")]]))

    async def _ask_model(self, name: str, runner: str, new: bool) -> None:
        if runner == "claude":
            models = [m for m, _ in CLAUDE_MODELS]
            labels = [label for _, label in CLAUDE_MODELS]
        else:
            models = (await opencode_models(self.opencode.opencode_bin))[:30]
            labels = [("🆓 " if is_free_model(m) else "") + m.removeprefix("opencode/") for m in models]
        tok = self._token({"action": "agent_model", "name": name, "runner": runner, "new": new, "models": models})
        buttons = [InlineKeyboardButton(truncate(label, 40), callback_data=f"agm:{tok}:{i}")
                   for i, label in enumerate(labels)]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("✏️ Digitar outro", callback_data=f"agm:{tok}:x")])
        extra = "" if models else "\n(não consegui listar os modelos do opencode; digite o nome)"
        await self.notify(f"Modelo do agente {name} ({runner_label(runner)}):{extra}",
                          markup=InlineKeyboardMarkup(rows))

    async def _save_agent_model(self, name: str, runner: str, model: str, new: bool) -> None:
        agent = self.agents.get(name) or Agent(name=name)
        agent.runner, agent.model = runner, model
        try:
            self.agents.put(agent)
        except ValueError as e:
            await self.notify(f"❌ {e}")
            return
        free = " 🆓 (grátis)" if is_free_model(model) else ""
        await self.notify(f"✅ {name}: {runner_label(runner)} / {model or 'padrão'}{free}")
        if new:
            await self._ask_instructions(name, new=True)
        else:
            await self._show_agent(agent)

    async def _ask_instructions(self, name: str, new: bool) -> None:
        self._awaiting = {"kind": "agent_instructions", "name": name, "ts": time.time()}
        agent = self.agents.get(name)
        current = f"\n\nAtuais: {truncate(agent.instructions, 500)}" if agent and agent.instructions else ""
        buttons = [InlineKeyboardButton("⏭️ Pular" if new else "Cancelar", callback_data=f"agi:skip:{name}")]
        if not new and agent and agent.instructions:
            buttons.append(InlineKeyboardButton("🧹 Limpar", callback_data=f"agi:clear:{name}"))
        await self.notify(f"📝 Envie agora, numa mensagem, as instruções do agente {name}: papel, estilo e regras.\n"
                          "Ex.: \"Você é um revisor de código rigoroso. Aponte bugs e sugira testes.\"\n"
                          f"(A próxima mensagem de texto será usada como instruções.){current}",
                          markup=InlineKeyboardMarkup([buttons]))

    async def _save_instructions(self, name: str, text: str) -> None:
        agent = self.agents.get(name)
        if not agent:
            await self.notify(f"Agente '{name}' não encontrado.")
            return
        agent.instructions = redact(text, self.secrets).strip()
        self.agents.put(agent)
        await self.notify("✅ Instruções salvas.")
        await self._show_agent(agent)

    async def _confirm_delete_agent(self, name: str) -> None:
        tok = self._token({"action": "agent_delete", "name": name})
        await self.notify(f"Apagar o agente '{name}'? Projetos que o usam voltam ao runner padrão.",
                          markup=InlineKeyboardMarkup([[
                              InlineKeyboardButton("🗑️ Apagar", callback_data=f"ok:{tok}"),
                              InlineKeyboardButton("Cancelar", callback_data=f"no:{tok}")]]))

    async def _handle_awaiting(self, text: str) -> bool:
        """Entrega o texto ao assistente que o pediu. False = não havia nada esperando."""
        aw, self._awaiting = self._awaiting, None
        if not aw or time.time() - aw["ts"] > AWAIT_TTL:
            return False
        if aw["kind"] == "agent_instructions":
            await self._save_instructions(aw["name"], text)
        elif aw["kind"] == "agent_model":
            model = text.strip()
            if " " in model or len(model) > 100:
                self._awaiting = {**aw, "ts": time.time()}
                await self.notify("Nome de modelo inválido. Envie só o identificador, ex.: opencode/big-pickle")
                return True
            await self._save_agent_model(aw["name"], aw["runner"], model, aw["new"])
        return True

    async def _agent_callback(self, data: str) -> None:
        kind, _, rest = data.partition(":")
        if kind in ("ag", "agi"):
            action, _, name = rest.partition(":")
            if kind == "ag" and action == "none":
                await self._use_agent(None)
                return
            agent = self.agents.get(name)
            if not agent:
                await self.notify("Agente não encontrado (foi apagado?). Veja /agentes.")
                return
            if kind == "agi":
                self._awaiting = None
                if action == "clear":
                    agent.instructions = ""
                    self.agents.put(agent)
                    await self.notify("🧹 Instruções removidas.")
                await self._show_agent(agent)
            elif action == "show":
                await self._show_agent(agent)
            elif action == "use":
                await self._use_agent(agent.name)
            elif action == "model":
                await self._ask_runner(agent.name, new=False)
            elif action == "instr":
                await self._ask_instructions(agent.name, new=False)
            elif action == "del":
                await self._confirm_delete_agent(agent.name)
            return
        tok, _, choice = rest.partition(":")
        item = self.pending.pop(tok, None)
        if not item:
            await self.notify("Este pedido expirou; comece de novo com /agentes.")
            return
        if kind == "agr" and choice in RUNNERS:
            if choice == "opencode" and not self.opencode.opencode_bin:
                await self.notify("⚠️ opencode CLI não encontrado nesta máquina. Instale com: npm i -g opencode-ai")
                return
            await self._ask_model(item["name"], choice, item["new"])
        elif kind == "agm":
            if choice == "x":
                self._awaiting = {"kind": "agent_model", "name": item["name"], "runner": item["runner"],
                                  "new": item["new"], "ts": time.time()}
                example = "sonnet, opus, claude-sonnet-4-5" if item["runner"] == "claude" else "opencode/big-pickle"
                await self.notify(f"Envie o nome do modelo (ex.: {example}).")
                return
            try:
                model = item["models"][int(choice)]
            except (ValueError, IndexError):
                await self.notify("Opção inválida; use /agente modelo NOME.")
                return
            await self._save_agent_model(item["name"], item["runner"], model, item["new"])

    @owner_only
    async def cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logf = self.cfg.log_dir / "teleagent.log"
        tail = ""
        if logf.exists():
            with open(logf, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 6000))
                tail = f.read().decode("utf-8", "replace")
        tasks_dir = self.cfg.log_dir / "tasks"
        last = sorted(tasks_dir.glob("*.jsonl"))[-1:] if tasks_dir.exists() else []
        text = "📜 Final do log:\n" + (tail[-3500:] or "(vazio)")
        if last:
            text += f"\n\nÚltimo log de tarefa: {last[0]}"
        await self.notify(text)

    @owner_only
    async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("Comando desconhecido. /help")

    @owner_only
    async def on_other(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text("Por enquanto só aceito texto. Envie a tarefa por escrito.")

    @owner_only
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        if self._awaiting and await self._handle_awaiting(text):
            return
        if await self._busy(update):
            return
        cur = await self._require_project(update)
        if not cur:
            return
        self._start_task(cur[0], cur[1], text, kind="task", display=text)

    async def _busy(self, update: Update) -> bool:
        if self.task and not self.task.done():
            await update.effective_message.reply_text(
                f"⏳ Já há uma tarefa em andamento em {self.task_meta.get('project')}. "
                "Aguarde, veja /status ou cancele com /parar.")
            return True
        return False

    # ------------------------------------------------------------ callbacks

    @owner_only
    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = q.data or ""
        await q.answer()
        if data.startswith("sel:"):
            try:
                name = self._project_cache[int(data[4:])]
            except (ValueError, IndexError):
                await q.edit_message_text("Lista expirada; use /projetos novamente.")
                return
            await q.edit_message_reply_markup(None)
            await self._select_project(name)
            return
        if data.startswith("mk:"):
            _, tok, idx = data.split(":", 2)
            item = self.pending.pop(tok, None)
            await q.edit_message_reply_markup(None)
            try:
                root = self.cfg.projects_dirs[int(idx)]
            except (ValueError, IndexError):
                item = None
            if not item:
                await self.notify("Este pedido expirou; use /criar novamente.")
                return
            await self._create_project(root, item["name"])
            return
        if data.startswith(("ag:", "agr:", "agm:", "agi:")):
            await q.edit_message_reply_markup(None)
            await self._agent_callback(data)
            return
        verb, _, tok = data.partition(":")
        item = self.pending.pop(tok, None)
        if not item:
            await q.edit_message_reply_markup(None)
            await self.notify("Este pedido de confirmação expirou ou já foi usado.")
            return
        await q.edit_message_reply_markup(None)
        if verb == "no":
            if item["action"] == "approve":
                await self._record_refusal(item)
            await self.notify("❌ Cancelado.")
            return
        action = item["action"]
        if action in ("approve", "install", "publish", "clone") and self.task and not self.task.done():
            self.pending[tok] = item
            await self.notify("⏳ Há uma tarefa em andamento; tente novamente quando ela terminar.")
            return
        if action == "approve":
            await self._approve(item)
        elif action == "install":
            await self._install(item["project"])
        elif action == "publish":
            await self._publish(item["project"], item["message"])
        elif action == "clone":
            await self._clone(item["name"], item["url"])
        elif action == "agent_delete":
            self.agents.delete(item["name"])
            self.state.forget_agent(item["name"])
            await self.notify(f"🗑️ Agente '{item['name']}' apagado.")
        elif action == "shutdown":
            await self.notify("⏻ Desligando: sincronizando memória e liberando a instância…")
            self.app.stop_running()

    # ------------------------------------------------------------ tarefas

    def _start_task(self, name: str, path: Path, task_text: str, *, kind: str, display: str,
                    resume: str | None = None, extra_allowed: list[str] | None = None,
                    extra_dirs: list[str] | None = None, task_header: str = "Tarefa do proprietário:") -> None:
        self.task_meta = {"project": name, "display": display, "started": now_iso()}
        self.task = asyncio.create_task(self._run_task(name, path, task_text, kind=kind, display=display,
                                                       resume=resume, extra_allowed=extra_allowed,
                                                       extra_dirs=extra_dirs, task_header=task_header))

    async def _run_task(self, name: str, path: Path, task_text: str, *, kind: str, display: str,
                        resume: str | None, extra_allowed: list[str] | None, task_header: str,
                        extra_dirs: list[str] | None = None) -> None:
        active_runner, model, agent = self._active_profile(name)
        st = self.memory.load(name)
        mem_task = self.memory.start_task(st, display, kind)
        self.memory.save(name, st)
        if resume is None:
            resume = self.state.session_for(name, active_runner)
        self.state.running_task = {"project": name, "task_id": mem_task["id"], "prompt": display,
                                   "started": mem_task["started"]}
        prompt = PROMPT_TEMPLATE.format(name=name, path=path, machine=self.cfg.machine_name,
                                        context=render_context(st), task_header=task_header, task=task_text,
                                        memory_instructions=MEMORY_INSTRUCTIONS,
                                        agent_section=_agent_section(agent))
        who = f" [{agent.name}]" if agent else ""
        status_msg = await self.notify(f"⏳ Executando em {name}{who}…\n{truncate(display, 200)}")
        last_edit = [0.0]

        def render(p: Progress) -> str:
            el = int(time.monotonic() - p.started)
            txt = f"⏳ {name} • {el // 60}m{el % 60:02d}s • {p.tool_calls} ações\n▶ {truncate(p.last_action, 200)}"
            if p.last_text:
                txt += f"\n💬 {truncate(p.last_text, 300)}"
            return txt

        tick = asyncio.create_task(asyncio.sleep(0))  # placeholder

        if active_runner == "opencode":
            from .opencode_runner import OpenCodeProgress

            def render_oc(p: OpenCodeProgress) -> str:
                el = int(time.monotonic() - p.started)
                txt = f"⏳ {name} [opencode] • {el // 60}m{el % 60:02d}s • {p.tool_calls} ações\n▶ {truncate(p.last_action, 200)}"
                if p.last_text:
                    txt += f"\n💬 {truncate(p.last_text, 300)}"
                return txt

            async def on_progress_oc(p: OpenCodeProgress):
                if time.monotonic() - last_edit[0] >= 5:
                    last_edit[0] = time.monotonic()
                    await self.edit(status_msg, render_oc(p))

            async def ticker_oc():
                while True:
                    await asyncio.sleep(30)
                    if self.opencode.progress and time.monotonic() - last_edit[0] >= 25:
                        last_edit[0] = time.monotonic()
                        await self.edit(status_msg, render_oc(self.opencode.progress))

            tick = asyncio.create_task(ticker_oc())
            oc_result: OpenCodeResult | None = None
            try:
                oc_result = await self.opencode.run(
                    path, prompt, session_id=resume, auto=False,
                    model=model or None, on_progress=on_progress_oc, log_name=_safe_name(name),
                )
                if resume and oc_result.is_error and not oc_result.tool_calls and not oc_result.cancelled:
                    # Sessão antiga inválida (ex.: pasta do projeto mudou): recomeça com a memória.
                    log.info("Falha ao retomar sessão opencode %s; iniciando sessão nova", resume)
                    self.state.set_session(name, None)
                    oc_result = await self.opencode.run(
                        path, prompt, auto=False,
                        model=model or None, on_progress=on_progress_oc, log_name=_safe_name(name),
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("Falha ao executar o opencode")
                oc_result = OpenCodeResult(is_error=True, text=f"Falha ao executar o opencode: {e}")
            finally:
                tick.cancel()

            # Normaliza para o formato comum usado pelo restante do método.
            if oc_result.session_id and not oc_result.cancelled:
                self.state.set_session(name, oc_result.session_id, "opencode")
            clean_text, block = extract_memory_block(oc_result.text)
            status = oc_result.status
            st = self.memory.load(name)
            self.memory.finish_task(st, mem_task["id"], status, clean_text, block, oc_result.cost)
            self.memory.save(name, st)
            self.state.running_task = None

            icon = {"success": "✅", "cancelled": "🛑", "timeout": "⌛", "error": "❌"}.get(status, "ℹ️")
            label = {"success": "Concluído", "cancelled": "Cancelado", "timeout": "Tempo esgotado", "error": "Erro"}[status]
            meta = [f"{oc_result.tool_calls} ações", f"${oc_result.cost:.4f}" if oc_result.cost else "grátis"]
            await self.edit(status_msg, f"{icon} {label} em {name} [opencode] • " + " • ".join(meta))
            body = clean_text.strip() or "(sem texto de resposta)"
            if oc_result.timed_out:
                body = f"Tarefa excedeu o tempo limite. Use /continuar para retomar.\n\n" + body
            if oc_result.cancelled:
                body = "Tarefa cancelada. Use /continuar para retomar."
            await self.send_long(f"{icon} {label} — {name}\n\n{body}")
            # Mostra as ferramentas negadas (sem botão de aprovação: opencode não tem --resume com regras)
            if oc_result.denials and not oc_result.cancelled:
                lines = ["⛔ O opencode tentou, mas foi bloqueado em:"]
                for d in oc_result.denials[:5]:
                    lines.append(f"• {d.tool}: {json.dumps(d.input)[:120]}")
                lines.append("\nPara liberar, use /runner claude e reenvie a tarefa.")
                await self.notify("\n".join(lines))
            rep = await self.sync.sync(f"TeleAgent: {status} em {name}")
            if not rep.ok:
                await self.notify(f"⚠️ Memória salva localmente, mas a sincronização falhou: {rep.message}")
            return

        # ── Claude Code (runner padrão) ──────────────────────────────────────

        async def on_progress(p: Progress):
            if time.monotonic() - last_edit[0] >= 5:
                last_edit[0] = time.monotonic()
                await self.edit(status_msg, render(p))

        async def ticker():
            while True:
                await asyncio.sleep(30)
                if self.runner.progress and time.monotonic() - last_edit[0] >= 25:
                    last_edit[0] = time.monotonic()
                    await self.edit(status_msg, render(self.runner.progress))

        tick = asyncio.create_task(ticker())
        result: RunResult | None = None
        try:
            result = await self.runner.run(path, prompt, resume=resume, extra_allowed=extra_allowed,
                                           extra_dirs=extra_dirs, model=model or None, on_progress=on_progress,
                                           log_name=_safe_name(name))
            if resume and result.status == "error" and not result.num_turns and not result.cancelled:
                # Sessão inexistente nesta máquina (ou corrompida): recomeça com a memória.
                log.info("Falha ao retomar sessão %s; iniciando sessão nova", resume)
                self.state.set_session(name, None)
                result = await self.runner.run(path, prompt, extra_allowed=extra_allowed, extra_dirs=extra_dirs,
                                               model=model or None, on_progress=on_progress,
                                               log_name=_safe_name(name))
        except Exception as e:  # noqa: BLE001
            log.exception("Falha ao executar o Claude Code")
            result = RunResult(is_error=True, text=f"Falha ao executar o Claude Code: {e}")
        finally:
            tick.cancel()

        if result.session_id and not result.cancelled:
            self.state.set_session(name, result.session_id, "claude")
        clean_text, block = extract_memory_block(result.text)
        status = result.status
        st = self.memory.load(name)
        self.memory.finish_task(st, mem_task["id"], status, clean_text, block, result.cost_usd)
        self.memory.save(name, st)
        self.state.running_task = None

        icon = {"done": "✅", "cancelled": "🛑", "timeout": "⌛", "error": "❌"}.get(status, "ℹ️")
        label = {"done": "Concluído", "cancelled": "Cancelado", "timeout": "Tempo esgotado", "error": "Erro"}[status]
        meta = [f"{int(result.duration_s // 60)}m{int(result.duration_s % 60):02d}s", f"{result.tool_calls} ações"]
        await self.edit(status_msg, f"{icon} {label} em {name} • " + " • ".join(meta))
        body = clean_text.strip() or ("(sem texto de resposta)" if status == "done" else result.stderr_tail[-1500:])
        if result.timed_out:
            body = f"A tarefa excedeu {self.cfg.task_timeout}s e foi encerrada. Use /continuar para retomar.\n\n" + body
        if result.cancelled:
            body = "Tarefa cancelada. O progresso parcial foi registrado na memória; use /continuar para retomar."
        await self.send_long(f"{icon} {label} — {name}\n\n{body}")
        if result.denials and not result.cancelled:
            await self._offer_approvals(name, path, result, mem_task["id"])
        rep = await self.sync.sync(f"TeleAgent: {status} em {name}")
        if not rep.ok:
            await self.notify(f"⚠️ Memória salva localmente, mas a sincronização falhou: {rep.message}")

    async def _offer_approvals(self, name: str, path: Path, result: RunResult, task_id: str) -> None:
        reviews, seen = [], set()
        for d in result.denials:
            r = review_denial(d, path)
            key = (r.tool, r.summary)
            if key not in seen:
                seen.add(key)
                reviews.append(r)
        offer = [r for r in reviews if r.offer]
        blocked = [r for r in reviews if not r.offer]
        lines = ["🔐 O Claude Code pediu ações que exigem sua confirmação:"]
        for i, r in enumerate(offer, 1):
            lines.append(f"{i}. [{r.category}] {r.tool}: {r.summary}")
        if blocked:
            lines.append("\n⛔ Não podem ser aprovadas pelo Telegram:")
            lines += [f"• {r.tool}: {r.summary} — {r.reason}" for r in blocked]
        if not offer:
            await self.notify("\n".join(lines))
            return
        if not result.session_id:
            lines.append("\n(Sem ID de sessão para retomar; reenvie a tarefa.)")
            await self.notify("\n".join(lines))
            return
        dirs = list(dict.fromkeys(d for r in offer for d in r.dirs))
        if dirs:
            lines.append("\n📂 Pastas fora do projeto que serão acessadas: " + ", ".join(dirs))
        lines.append("\nAprovar libera SOMENTE esses comandos exatos, numa continuação da mesma sessão.")
        tok = self._token({"action": "approve", "project": name, "session": result.session_id,
                           "rules": [r.rule for r in offer], "summaries": [f"{r.tool}: {r.summary}" for r in offer],
                           "dirs": dirs, "task_id": task_id})
        await self.notify("\n".join(lines), markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Aprovar e continuar", callback_data=f"ok:{tok}"),
            InlineKeyboardButton("❌ Recusar", callback_data=f"no:{tok}")]]))

    async def _approve(self, item: dict) -> None:
        try:
            path = resolve_project(item["project"], self.cfg.projects_dirs, self.excluded)
        except PathError as e:
            await self.notify(f"❌ {e}")
            return
        if self._active_profile(item["project"])[0] != "claude":
            await self.notify("⚠️ O projeto agora usa o OpenCode; aprovações só retomam sessões do Claude Code. "
                              "Selecione um agente Claude (/agentes) e reenvie a tarefa.")
            return
        listing = "\n".join(f"- {s}" for s in item["summaries"])
        task = (f"O proprietário APROVOU as seguintes ações que foram negadas antes:\n{listing}\n"
                "Execute exatamente essas ações (sem variações) e conclua a tarefa original.")
        self._start_task(item["project"], path, task, kind="approval", display="Aprovação: " + "; ".join(item["summaries"]),
                         resume=item["session"], extra_allowed=item["rules"], extra_dirs=item.get("dirs"),
                         task_header="Continuação:")

    async def _record_refusal(self, item: dict) -> None:
        st = self.memory.load(item["project"])
        self.memory.add_item(st, "decision", "Proprietário recusou: " + "; ".join(item["summaries"]))
        self.memory.save(item["project"], st)

    # ------------------------------------------------------------ ações confirmadas

    async def _install(self, name: str) -> None:
        try:
            path = resolve_project(name, self.cfg.projects_dirs, self.excluded)
        except PathError as e:
            await self.notify(f"❌ {e}")
            return
        steps = deps.detect(path)
        msg = await self.notify(f"📦 Instalando dependências de {name}…")
        self.task_meta = {"project": name, "display": "instalação de dependências"}
        logf = self.cfg.log_dir / "tasks" / f"{time.strftime('%Y%m%d-%H%M%S')}-install-{_safe_name(name)}.log"
        self.task = asyncio.create_task(deps.run_steps(path, steps, logf))
        try:
            results = await self.task
        except asyncio.CancelledError:
            await self.notify(f"🛑 Instalação cancelada. Log: {logf}")
            return
        ok = all(code == 0 for _, code, _ in results) and len(results) == len(steps)
        lines = [f"{'✅' if ok else '❌'} Instalação em {name}:"]
        for step, code, tail in results:
            lines.append(f"{'✔' if code == 0 else '✖'} {step.display} (código {code})")
            if code != 0:
                lines.append(truncate(tail, 1500))
        lines.append(f"Log: {logf}")
        await self.edit(msg, f"{'✅' if ok else '❌'} Instalação finalizada")
        await self.notify("\n".join(lines))
        st = self.memory.load(name)
        self.memory.add_item(st, "note" if ok else "pending",
                             f"Dependências {'instaladas' if ok else 'com falha na instalação'} em "
                             f"{self.cfg.machine_name}: " + ", ".join(s.display for s in steps))
        self.memory.save(name, st)
        await self.sync.sync(f"TeleAgent: instalação em {name}")

    async def _publish(self, name: str, message: str) -> None:
        try:
            path = resolve_project(name, self.cfg.projects_dirs, self.excluded)
        except PathError as e:
            await self.notify(f"❌ {e}")
            return
        out = []
        await git(path, "add", "-A")
        names = (await git(path, "diff", "--cached", "--name-only")).out.splitlines()
        bad = [n for n in names if SENSITIVE_FILENAMES.search(n)]
        diff = (await git(path, "diff", "--cached", "-U0", "--no-color")).out
        added = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        if bad or contains_secret(added):
            await git(path, "reset", "-q")
            await self.notify("⛔ Publicação bloqueada: possível segredo nas alterações"
                              + (f" ({', '.join(bad[:5])})" if bad else "") + ". Nada foi commitado.")
            return
        if names:
            c = await git(path, "commit", "-q", "-m", message)
            out.append("commit: " + ("ok" if c.ok else c.err[-300:]))
            if not c.ok:
                await self.notify("❌ " + "\n".join(out))
                return
        info = await project_git_info(path)
        args = ["push"] if info.get("upstream") else ["push", "-u", "origin", info.get("branch", "HEAD")]
        p = await git(path, *args, timeout=300)
        out.append("push: " + ("ok" if p.ok else p.err[-800:]))
        await self.notify(f"{'✅' if p.ok else '❌'} Publicação de {name}\n" + "\n".join(out))
        if p.ok:
            st = self.memory.load(name)
            self.memory.add_item(st, "done", f"Código publicado ({info.get('branch')}): {message}")
            self.memory.save(name, st)
            await self.sync.sync(f"TeleAgent: publicação em {name}")

    async def _clone(self, name: str, url: str) -> None:
        if not PROJECT_NAME_RE.match(name):
            await self.notify("Nome de projeto inválido.")
            return
        dest = self.cfg.projects_dirs[0] / name
        if dest.exists():
            await self.notify(f"{dest} já existe.")
            return
        await self.notify(f"📥 Clonando {url}…")
        r = await git(self.cfg.projects_dirs[0], "clone", url, name, timeout=900)
        if r.ok:
            await self.notify(f"✅ Clonado em {dest}.")
            await self._select_project(name)
        else:
            await self.notify(f"❌ Falha ao clonar: {r.err[-800:]}")

    # ------------------------------------------------------------ erros

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, Conflict):
            log.error("409 Conflict: outra instância está fazendo polling deste bot.")
            await self.notify(f"⚠️ Outra instância está usando este bot. A instância em "
                              f"'{self.cfg.machine_name}' será encerrada para evitar duplicidade.")
            if self.app:
                self.app.stop_running()
            return
        if isinstance(err, NetworkError):
            log.warning("Erro de rede (o polling tentará novamente): %s", err)
            return
        log.error("Erro não tratado", exc_info=err)
        if isinstance(update, Update) and update.effective_user and update.effective_user.id == self.cfg.owner_id:
            await self.notify(f"❌ Erro interno: {type(err).__name__}: {truncate(str(err), 500)}")


def _agent_section(agent: Agent | None) -> str:
    if not agent or not agent.instructions:
        return ""
    return (f"\nVocê é o agente \"{agent.name}\". Instruções do agente definidas pelo proprietário "
            f"(seguem valendo junto com as regras acima):\n{agent.instructions}\n")


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]
