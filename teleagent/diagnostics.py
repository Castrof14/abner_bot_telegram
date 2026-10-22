"""`python -m teleagent check` — verifica configuração, dependências e acessos."""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys

from .config import BASE_DIR, ENV_FILE, ConfigError, load_config


def _line(ok: bool | None, text: str) -> None:
    mark = {True: "✔", False: "✖", None: "•"}[ok]
    print(f" {mark} {text}")


async def run_check() -> int:
    failures = 0
    print("Diagnóstico do TeleAgent\n")
    _line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]} (requer 3.10+)")
    try:
        import dotenv  # noqa: F401
        import telegram
        _line(True, f"python-telegram-bot {telegram.__version__}")
    except ImportError as e:
        _line(False, f"dependência ausente: {e} — rode pip install -r requirements.txt")
        return 1
    _line(bool(shutil.which("git")), f"git: {shutil.which('git') or 'NÃO encontrado'}")

    gi = BASE_DIR / ".gitignore"
    _line(gi.exists() and ".env" in gi.read_text(encoding="utf-8").splitlines(), ".gitignore protege .env")
    try:
        cfg = load_config()
        _line(True, f"{ENV_FILE} válido")
    except ConfigError as e:
        _line(False, f"configuração: {e}")
        return 1
    if os.name != "nt":
        mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
        _line(mode & 0o077 == 0, f".env com permissão {oct(mode)} (recomendado 0o600)")
    _line(True, f"proprietário autorizado: {cfg.owner_id}")
    for d in cfg.projects_dirs:
        _line(d.is_dir(), f"projetos: {d}")

    from .claude_runner import ClaudeRunner
    runner = ClaudeRunner(cfg.claude_bin, cfg.allowed_tools, cfg.denied_tools)
    _line(bool(cfg.claude_bin), f"Claude Code: {cfg.claude_bin or 'NÃO encontrado (defina CLAUDE_BIN)'}")
    failures += 0 if cfg.claude_bin else 1
    if cfg.claude_bin:
        _line(None, f"versão: {await runner.version()}")
        auth = await runner.auth_status()
        if auth is None:
            _line(None, "não foi possível ler `claude auth status`")
        else:
            _line(bool(auth.get("loggedIn")), f"login do Claude Code: {auth.get('authMethod', '?')}"
                  if auth.get("loggedIn") else "Claude Code NÃO autenticado (rode `claude` e faça login)")

    if cfg.sync_repo_url:
        from .gitsync import git
        r = await git(BASE_DIR, "ls-remote", cfg.sync_repo_url, timeout=60)
        _line(r.ok, f"repositório de memória acessível: {cfg.sync_repo_url}" if r.ok
              else f"sem acesso ao repositório de memória: {r.err[-200:]}")
        failures += 0 if r.ok else 1
    else:
        _line(None, "SYNC_REPO_URL vazio: memória apenas local")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(f"https://api.telegram.org/bot{cfg.telegram_token}/getMe")
            data = resp.json()
        _line(data.get("ok"), f"Telegram: @{data['result']['username']}" if data.get("ok")
              else f"Telegram recusou o token: {data.get('description')}")
        failures += 0 if data.get("ok") else 1
    except httpx.HTTPError as e:
        _line(False, f"Telegram inacessível: {type(e).__name__}")
        failures += 1

    from .instance_lock import Lease
    lease = Lease(cfg.sync_dir, cfg.machine_name, cfg.lease_stale_hours).read()
    if lease:
        _line(None, f"instância registrada: {lease.get('machine')} (liberada={lease.get('released')}, "
                    f"heartbeat {lease.get('heartbeat')})")
    print("\nResultado:", "OK" if failures == 0 else f"{failures} problema(s)")
    return 0 if failures == 0 else 1
