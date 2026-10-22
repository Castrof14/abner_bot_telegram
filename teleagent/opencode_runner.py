"""Execução do opencode CLI em modo não interativo.

Interface usada (verificada com `opencode --help`, versão testada: 1.18.x):
  opencode run <mensagem> --format json --dir <cwd> --model <provider/model>
               [--session <id>] [--auto]

O opencode não tem sistema de permissões por ferramenta como o Claude Code.
Rodamos sem `--auto` (permissões negadas por padrão) para tarefas normais; quando
o usuário aprova uma execução, rodamos com `--auto` restrito ao diretório do projeto.

Formato de eventos de saída (JSON por linha):
  {"type": "text",        "part": {"type":"text","text":"..."}}
  {"type": "tool_use",    "part": {"type":"tool","tool":"...","state":{"status":"error|completed","error":"...","input":{...}}}}
  {"type": "step_finish", "part": {"type":"step-finish","reason":"stop|tool-calls","cost":0,...}}

Quando `tool_use.state.status == "error"` e o erro contém "rejected permission",
a ferramenta foi negada pelo opencode (sem `--auto`).
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import platform
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

STRIP_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID")

REJECTION_MARKER = "rejected permission"


def find_opencode_bin(explicit: str | None = None) -> str | None:
    if explicit:
        p = Path(explicit).expanduser()
        found = str(p) if p.exists() else shutil.which(explicit)
        if found:
            return found
    on_path = shutil.which("opencode")
    if on_path:
        return on_path
    home = Path.home()
    exe = "opencode.exe" if platform.system() == "Windows" else "opencode"
    candidates = [
        home / ".local" / "bin" / exe,
        Path("/opt/homebrew/bin/opencode"),
        Path("/usr/local/bin/opencode"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


@dataclass
class OpenCodeProgress:
    started: float = field(default_factory=time.monotonic)
    session_id: str = ""
    tool_calls: int = 0
    last_action: str = ""
    last_text: str = ""


@dataclass
class OpenCodeDenial:
    """Uma ferramenta negada pelo opencode (sem --auto)."""
    tool: str
    input: dict
    error: str


@dataclass
class OpenCodeResult:
    session_id: str = ""
    text: str = ""
    is_error: bool = False
    cost: float = 0.0
    denials: list[OpenCodeDenial] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    exit_code: int | None = None
    stderr_tail: str = ""
    log_path: Path | None = None
    tool_calls: int = 0
    error_message: str = ""

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.timed_out:
            return "timeout"
        if self.is_error:
            return "error"
        return "success"


OpenCodeCallback = Callable[[OpenCodeProgress], Awaitable[None]]


class OpenCodeRunner:
    def __init__(self, opencode_bin: str | None, model: str = "",
                 timeout: int = 3600, log_dir: Path | None = None):
        self.opencode_bin = opencode_bin or find_opencode_bin()
        self.model = model
        self.timeout = timeout
        self.log_dir = log_dir
        self._proc: asyncio.subprocess.Process | None = None
        self._cancel_requested = False
        self.progress: OpenCodeProgress | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def build_args(self, *, session_id: str | None = None,
                   auto: bool = False, model: str | None = None) -> list[str]:
        if not self.opencode_bin:
            raise FileNotFoundError("opencode CLI não encontrado. Instale com: npm i -g opencode-ai")
        args = [self.opencode_bin, "run", "--format", "json"]
        m = model or self.model
        if m:
            args += ["--model", m]
        if session_id:
            args += ["--session", session_id, "--fork"]
        if auto:
            args += ["--auto"]
        return args

    async def version(self) -> str:
        try:
            bin_ = self.opencode_bin or find_opencode_bin()
            if not bin_:
                return "não instalado"
            proc = await asyncio.create_subprocess_exec(
                bin_, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 30)
            return (out or err).decode().strip()
        except Exception as e:
            return f"indisponível ({e})"

    async def run(self, cwd: Path, prompt: str, *,
                  session_id: str | None = None,
                  auto: bool = False,
                  model: str | None = None,
                  on_progress: OpenCodeCallback | None = None,
                  log_name: str = "task") -> OpenCodeResult:
        if self.running:
            raise RuntimeError("Já existe uma execução do opencode em andamento.")
        args = self.build_args(session_id=session_id, auto=auto, model=model)
        args.append(prompt)         # mensagem passada como argumento posicional
        args += ["--dir", str(cwd)]

        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        result = OpenCodeResult()
        log_path = None
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{log_name}.jsonl"
            result.log_path = log_path

        self._cancel_requested = False
        started = time.monotonic()
        self.progress = OpenCodeProgress(started=started, session_id=session_id or "")

        kwargs: dict = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True

        log.info("opencode em %s (session=%s model=%s auto=%s)", cwd, session_id, model or self.model, auto)
        self._proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        proc = self._proc
        stderr_chunks: list[bytes] = []
        log_lines: list[str] = []
        text_parts: list[str] = []

        async def read_stderr():
            async for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        async def read_stdout():
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if log_path:
                    log_lines.append(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = ev.get("type", "")
                part = ev.get("part") or {}
                sid = ev.get("sessionID", "")
                if sid and not result.session_id:
                    result.session_id = sid
                    self.progress.session_id = sid

                if etype == "text":
                    t = part.get("text", "")
                    if t:
                        text_parts.append(t)
                        self.progress.last_text = t[-300:]

                elif etype == "tool_use":
                    result.tool_calls += 1
                    self.progress.tool_calls += 1
                    tool = part.get("tool", "?")
                    state = part.get("state") or {}
                    status = state.get("status", "")
                    self.progress.last_action = f"{tool} ({status})"

                    if status == "error":
                        err = state.get("error", "")
                        if REJECTION_MARKER in err.lower():
                            result.denials.append(OpenCodeDenial(
                                tool=tool,
                                input=state.get("input") or {},
                                error=err,
                            ))

                elif etype == "error":
                    err = ev.get("error") or {}
                    result.is_error = True
                    result.error_message = (err.get("data") or {}).get("message") or err.get("name") or "erro"

                elif etype == "step_finish":
                    cost = part.get("cost", 0) or 0
                    result.cost += cost

                if on_progress:
                    try:
                        await on_progress(self.progress)
                    except Exception:
                        pass

        async def watchdog():
            await asyncio.sleep(self.timeout)
            if proc.returncode is None:
                result.timed_out = True
                _terminate(proc)

        try:
            watcher = asyncio.create_task(watchdog())
            await asyncio.gather(read_stdout(), read_stderr())
            watcher.cancel()
            await proc.wait()
        except asyncio.CancelledError:
            result.cancelled = True
            _terminate(proc)
            await proc.wait()
        finally:
            self._proc = None

        result.exit_code = proc.returncode
        result.text = "".join(text_parts).strip()
        result.stderr_tail = b"".join(stderr_chunks[-10:]).decode("utf-8", errors="replace")[-500:]

        if log_path and log_lines:
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        if result.error_message and not result.text:
            result.text = f"O opencode retornou erro: {result.error_message}"
        if proc.returncode not in (0, None) and not result.text and not result.cancelled:
            result.is_error = True
            if result.stderr_tail:
                result.text = result.stderr_tail

        return result

    def cancel(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._cancel_requested = True
            _terminate(self._proc)


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "nt":
            import subprocess
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
