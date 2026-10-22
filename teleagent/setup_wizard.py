"""Assistente de configuração inicial (interativo, no terminal)."""
from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .config import ENV_FILE, find_claude_bin

API = "https://api.telegram.org/bot{token}/{method}"


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else (" [manter atual]" if default and secret else "")
    while True:
        value = (getpass.getpass(f"{prompt}{suffix}: ") if secret else input(f"{prompt}{suffix}: ")).strip()
        if value:
            return value
        if default:
            return default
        print("  (obrigatório)")


def _yes(prompt: str, default: bool = True) -> bool:
    v = input(f"{prompt} [{'S/n' if default else 's/N'}]: ").strip().lower()
    return default if not v else v in ("s", "sim", "y", "yes")


def _tg(token: str, method: str, **params) -> dict:
    r = httpx.post(API.format(token=token, method=method), json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", f"HTTP {r.status_code}"))
    return data["result"]


def _detect_owner(token: str, bot_username: str) -> int | None:
    print(f"\nAbra o Telegram e envie qualquer mensagem (ex.: /start) para @{bot_username}.")
    print("Aguardando até 2 minutos… (Ctrl+C para digitar o ID manualmente)")
    deadline = time.time() + 120
    offset = None
    try:
        while time.time() < deadline:
            params = {"timeout": 20, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            try:
                updates = _tg(token, "getUpdates", **params)
            except RuntimeError as e:
                if "Conflict" in str(e):
                    print("  ⚠️ Outra instância está fazendo polling deste bot. Pare-a ou informe o ID manualmente.")
                    return None
                raise
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                user = msg.get("from") or {}
                if msg.get("chat", {}).get("type") == "private" and user.get("id"):
                    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
                    print(f"  Mensagem recebida de: {name} (@{user.get('username', '-')}) — ID {user['id']}")
                    if _yes("  Este é você (o proprietário)?"):
                        _tg(token, "getUpdates", offset=offset, timeout=0)  # marca como lida
                        return int(user["id"])
    except KeyboardInterrupt:
        print()
    return None


def _write_env(values: dict[str, str]) -> None:
    existing = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    merged = {**{k: v or "" for k, v in existing.items()}, **values}
    example = (ENV_FILE.parent / ".env.example")
    lines, written = [], set()
    if example.exists():
        for line in example.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z0-9_]+)=", line)
            if m:
                key = m.group(1)
                lines.append(f"{key}={_quote(merged.get(key, ''))}")
                written.add(key)
            else:
                lines.append(line)
    for k, v in merged.items():
        if k not in written:
            lines.append(f"{k}={_quote(v)}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(ENV_FILE, 0o600)


def _quote(v: str) -> str:
    v = v or ""
    return f'"{v}"' if re.search(r"[\s#'\"]", v) else v


def run_wizard() -> int:
    print("=" * 60)
    print(" TeleAgent — configuração inicial")
    print("=" * 60)
    cur = dotenv_values(ENV_FILE) if ENV_FILE.exists() else {}
    if cur:
        print(f"Arquivo {ENV_FILE} existente: valores atuais serão oferecidos como padrão.")

    # 1. Token
    print("\n[1/5] Token do bot (crie com o @BotFather → /newbot). A digitação fica oculta.")
    while True:
        token = _ask("Token", cur.get("TELEGRAM_BOT_TOKEN") or "", secret=True)
        if not re.match(r"^\d{5,}:[A-Za-z0-9_-]{30,}$", token):
            print("  Formato inválido (esperado 123456789:ABC…).")
            continue
        try:
            me = _tg(token, "getMe")
            print(f"  ✔ Bot válido: @{me['username']} ({me['first_name']})")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ✖ Não foi possível validar o token: {e}")
            if not _yes("  Usar mesmo assim (ex.: sem internet agora)?", False):
                continue
            me = {"username": "seu_bot"}
            break

    # 2. Owner
    print("\n[2/5] ID numérico do proprietário (apenas ele poderá usar o bot).")
    owner = None
    if _yes("Detectar automaticamente enviando uma mensagem ao bot?"):
        try:
            owner = _detect_owner(token, me["username"])
        except Exception as e:  # noqa: BLE001
            print(f"  Falha na detecção: {e}")
    while not owner:
        raw = _ask("ID numérico (descubra com @userinfobot)", cur.get("TELEGRAM_OWNER_ID") or "")
        if raw.isdigit() and int(raw) > 0:
            owner = int(raw)
        else:
            print("  Deve ser um número positivo.")

    # 3. Projects
    print("\n[3/5] Diretório(s) de projetos autorizados. Vários: separe com ';'.")
    default_dir = cur.get("PROJECTS_DIRS") or str(Path.home() / "projetos")
    while True:
        raw = _ask("Diretório(s)", default_dir)
        dirs = [Path(p.strip()).expanduser() for p in raw.split(";") if p.strip()]
        ok = True
        for d in dirs:
            if d.resolve() in (Path.home().resolve(), Path(d.resolve().anchor)):
                print(f"  ✖ {d} é amplo demais (home ou raiz). Use uma pasta dedicada.")
                ok = False
            elif not d.is_dir():
                if _yes(f"  {d} não existe. Criar?"):
                    d.mkdir(parents=True, exist_ok=True)
                else:
                    ok = False
        if ok:
            break
    projects = ";".join(str(d.resolve()) for d in dirs)

    # 4. Sync repo
    print("\n[4/5] Repositório Git PRIVADO para sincronizar a memória entre computadores.")
    print("  Crie um repositório vazio e privado (GitHub/GitLab/Gitea…). Recomenda-se URL SSH")
    print("  (git@github.com:usuario/teleagent-memoria.git). Deixe vazio para usar só nesta máquina.")
    sync_url = input(f"URL [{cur.get('SYNC_REPO_URL') or 'vazio'}]: ").strip() or (cur.get("SYNC_REPO_URL") or "")
    if re.match(r"^https?://[^/]+:[^/]+@", sync_url):
        print("  ✖ Não coloque credenciais na URL. Use SSH ou o gerenciador de credenciais do Git.")
        sync_url = re.sub(r"^(https?://)[^/@]+@", r"\1", sync_url)
        print(f"  Usando: {sync_url}")
    if sync_url:
        print("  Testando acesso (git ls-remote)…")
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
        try:
            r = subprocess.run(["git", "ls-remote", sync_url], capture_output=True, text=True, timeout=60, env=env)
            if r.returncode == 0:
                print("  ✔ Acesso OK." + (" (repositório vazio)" if not r.stdout.strip() else ""))
            else:
                print(f"  ✖ Sem acesso: {r.stderr.strip()[-300:]}")
                print("    Configure a chave SSH/credenciais e rode o assistente de novo, ou ajuste SYNC_REPO_URL no .env.")
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"  ✖ Falha ao executar git: {e}")
        print("  ⚠️ Confirme que o repositório é PRIVADO.")
    branch = cur.get("SYNC_BRANCH") or "main"

    # 5. Machine + Claude
    print("\n[5/5] Esta máquina e o Claude Code.")
    machine = _ask("Nome desta máquina", cur.get("MACHINE_NAME") or socket.gethostname())
    claude = find_claude_bin(cur.get("CLAUDE_BIN") or None)
    if claude:
        print(f"  ✔ Claude Code encontrado: {claude}")
        try:
            v = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
            print(f"    versão: {v}")
            st = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30)
            try:
                logged = json.loads(st.stdout).get("loggedIn")
            except json.JSONDecodeError:
                logged = None
            if logged:
                print("    ✔ autenticado")
            else:
                print("    ✖ não autenticado: rode `claude` no terminal uma vez e faça login (ou `claude auth login`).")
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"    ✖ falha ao executar: {e}")
    else:
        print("  ✖ Claude Code CLI não encontrado. Instale: https://docs.claude.com/claude-code")
        print("    e depois defina CLAUDE_BIN no .env se ele não estiver no PATH.")
    claude_setting = cur.get("CLAUDE_BIN") or ""
    if claude and "native-binary" in claude:
        print("    ⚠️ Este é o binário da extensão da IDE (o caminho muda a cada atualização; o TeleAgent o")
        print("       redetecta sozinho). Para mais estabilidade, instale a CLI: https://docs.claude.com/claude-code")

    _write_env({
        "TELEGRAM_BOT_TOKEN": token, "TELEGRAM_OWNER_ID": str(owner), "PROJECTS_DIRS": projects,
        "SYNC_REPO_URL": sync_url, "SYNC_BRANCH": branch, "MACHINE_NAME": machine, "CLAUDE_BIN": claude_setting,
    })
    print(f"\n✔ Configuração salva em {ENV_FILE}" + (" (permissão 600)" if os.name != "nt" else ""))
    print("  Próximo passo: rode o diagnóstico (python -m teleagent check) e inicie o bot")
    print("  (./start.sh no Linux/macOS ou start.bat no Windows).")
    return 0
