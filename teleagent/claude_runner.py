"""Execução do Claude Code via sua CLI documentada (modo não interativo `-p`).

Interface usada (verificada com `claude --help`, versão 2.1.x):
  claude -p --output-format stream-json --verbose
         --permission-mode dontAsk --permission-prompts none
         --allowedTools ... --disallowedTools ...
         [--resume <session_id> | --session-id <uuid>] [--model ...] [--max-budget-usd ...]
  O prompt é enviado pela entrada padrão (sem shell e sem limite de argv).

`dontAsk` nega tudo que não estiver pré-aprovado; as negações chegam em
`permission_denials` no evento final `result` e viram pedidos de confirmação no
Telegram. As proteções do Claude Code NUNCA são desativadas
(sem --dangerously-skip-permissions / bypassPermissions).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .security import deny_rules_for_approved

log = logging.getLogger(__name__)

STRIP_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID")


@dataclass
class RunResult:
    session_id: str | None = None
    text: str = ""
    is_error: bool = False
    subtype: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_s: float = 0.0
    denials: list[dict] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    exit_code: int | None = None
    stderr_tail: str = ""
    log_path: Path | None = None
    tool_calls: int = 0

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.timed_out:
            return "timeout"
        if self.is_error or self.subtype not in ("success", ""):
            return "error"
        if self.exit_code not in (0, None) and not self.text:
            return "error"
        return "done"


@dataclass
class Progress:
    started: float
    tool_calls: int = 0
    last_action: str = "iniciando…"
    last_text: str = ""
    session_id: str | None = None


ProgressCallback = Callable[[Progress], Awaitable[None]]


class ClaudeNotFound(Exception):
    pass


def resolve_command(claude_bin: str | None) -> list[str]:
    if not claude_bin:
        raise ClaudeNotFound("Claude Code CLI não encontrado. Instale (https://docs.claude.com/claude-code) "
                             "ou defina CLAUDE_BIN no .env.")
    if os.name == "nt" and claude_bin.lower().endswith((".cmd", ".bat")):
        # Arquivos .cmd passam pelo cmd.exe, cuja interpretação de argumentos é insegura.
        # Executa o cli.js do pacote npm diretamente com o node.
        cli = Path(claude_bin).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        node = shutil.which("node")
        if cli.exists() and node:
            return [node, str(cli)]
        raise ClaudeNotFound("No Windows use o instalador nativo (claude.exe) ou defina CLAUDE_BIN para o claude.exe.")
    return [claude_bin]


def describe_tool(name: str, inp: dict) -> str:
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "path"):
        if inp.get(key):
            val = str(inp[key]).replace("\n", " ")
            return f"{name}: {val[:120]}"
    return name


class ClaudeRunner:
    def __init__(self, claude_bin: str | None, allowed: list[str], denied: list[str],
                 model: str = "", max_budget_usd: float | None = None, timeout: int = 3600,
                 log_dir: Path | None = None):
        self.claude_bin = claude_bin
        self.allowed = allowed
        self.denied = denied
        self.model = model
        self.max_budget = max_budget_usd
        self.timeout = timeout
        self.log_dir = log_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self.progress: Progress | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def build_args(self, *, resume: str | None = None, session_id: str | None = None,
                   extra_allowed: list[str] | None = None, extra_dirs: list[str] | None = None,
                   model: str | None = None) -> list[str]:
        extra_allowed = extra_allowed or []
        args = resolve_command(self.claude_bin) + [
            "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
        ]
        allowed = list(dict.fromkeys(self.allowed + extra_allowed))
        denied = deny_rules_for_approved(self.denied, extra_allowed)
        if allowed:
            args += ["--allowedTools", *allowed]
        if denied:
            args += ["--disallowedTools", *denied]
        if extra_dirs:
            args += ["--add-dir", *extra_dirs]
        model = model or self.model
        if model:
            args += ["--model", model]
        if self.max_budget:
            args += ["--max-budget-usd", f"{self.max_budget:g}"]
        if resume:
            args += ["--resume", resume]
        elif session_id:
            args += ["--session-id", session_id]
        return args

    async def version(self) -> str:
        try:
            cmd = resolve_command(self.claude_bin)
            proc = await asyncio.create_subprocess_exec(*cmd, "--version", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 30)
            return (out or err).decode().strip()
        except (ClaudeNotFound, OSError, asyncio.TimeoutError) as e:
            return f"indisponível ({e})"

    async def auth_status(self) -> dict | None:
        try:
            cmd = resolve_command(self.claude_bin)
            proc = await asyncio.create_subprocess_exec(*cmd, "auth", "status", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
            return json.loads(out.decode())
        except Exception:  # noqa: BLE001 — formato/versão podem variar
            return None

    async def run(self, cwd: Path, prompt: str, *, resume: str | None = None, session_id: str | None = None,
                  extra_allowed: list[str] | None = None, extra_dirs: list[str] | None = None,
                  model: str | None = None, on_progress: ProgressCallback | None = None,
                  log_name: str = "task") -> RunResult:
        if self.running:
            raise RuntimeError("Já existe uma execução do Claude Code em andamento.")
        args = self.build_args(resume=resume, session_id=session_id, extra_allowed=extra_allowed,
                               extra_dirs=extra_dirs, model=model)
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        result = RunResult()
        log_path = None
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{log_name}.jsonl"
            result.log_path = log_path
        self._cancel_requested = False
        started = time.monotonic()
        self.progress = Progress(started=started, session_id=resume or session_id)

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # permite encerrar a árvore de processos
        log.info("Claude Code em %s (resume=%s)", cwd, resume)
        self._proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024, **kwargs)
        proc = self._proc
        stderr_chunks: list[bytes] = []

        async def feed_stdin():
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            finally:
                proc.stdin.close()

        async def read_stderr():
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
                if sum(map(len, stderr_chunks)) > 256 * 1024:
                    stderr_chunks.pop(0)

        async def read_stdout():
            logf = open(log_path, "w", encoding="utf-8") if log_path else None
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").strip()
                    if not text:
                        continue
                    if logf:
                        logf.write(text + "\n")
                        logf.flush()
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if self._handle_event(event, result) and on_progress:
                        try:
                            await on_progress(self.progress)
                        except Exception:  # noqa: BLE001 — progresso nunca derruba a tarefa
                            log.debug("falha no callback de progresso", exc_info=True)
            finally:
                if logf:
                    logf.close()

        tasks = [asyncio.create_task(feed_stdin()), asyncio.create_task(read_stderr()),
                 asyncio.create_task(read_stdout())]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks[1:]), timeout=self.timeout)
            await proc.wait()
        except asyncio.TimeoutError:
            result.timed_out = True
            await self._terminate()
        except asyncio.CancelledError:
            result.cancelled = True
            await self._terminate()
            raise
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            result.exit_code = proc.returncode
            result.duration_s = time.monotonic() - started
            result.stderr_tail = b"".join(stderr_chunks).decode("utf-8", "replace")[-2000:]
            result.session_id = result.session_id or self.progress.session_id
            if self._cancel_requested:
                result.cancelled = True
            self._proc = None
        if not result.text and not result.cancelled and not result.timed_out and result.exit_code:
            result.is_error = True
            result.text = f"O Claude Code terminou com código {result.exit_code}.\n{result.stderr_tail[-1500:]}"
        return result

    def _handle_event(self, ev: dict, result: RunResult) -> bool:
        p = self.progress
        etype = ev.get("type")
        if ev.get("session_id"):
            p.session_id = ev["session_id"]
        if etype == "system" and ev.get("subtype") == "init":
            p.last_action = f"sessão iniciada ({ev.get('model', '')})"
            return True
        if etype == "system" and ev.get("subtype") == "permission_denied":
            p.last_action = f"⛔ negado: {ev.get('tool_name')}"
            return True
        if etype == "assistant":
            changed = False
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    p.tool_calls += 1
                    result.tool_calls = p.tool_calls
                    p.last_action = describe_tool(block.get("name", "?"), block.get("input") or {})
                    changed = True
                elif block.get("type") == "text" and block.get("text", "").strip():
                    p.last_text = block["text"].strip()
                    changed = True
            return changed
        if etype == "result":
            result.session_id = ev.get("session_id") or p.session_id
            result.text = ev.get("result") or ""
            result.is_error = bool(ev.get("is_error"))
            result.subtype = ev.get("subtype") or ""
            result.cost_usd = ev.get("total_cost_usd")
            result.num_turns = ev.get("num_turns")
            result.denials = ev.get("permission_denials") or []
            if result.subtype and result.subtype != "success" and not result.text:
                result.text = f"Execução terminou com status '{result.subtype}'."
                if ev.get("errors"):
                    result.text += "\n" + "\n".join(map(str, ev["errors"]))[:1500]
            return False
        return False

    async def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel_requested = True
        await self._terminate()
        return True

    async def _terminate(self) -> None:
        proc = self._proc
        if not proc or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                # Encerra a árvore inteira (inclui comandos que o agente iniciou).
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass
            await proc.wait()
