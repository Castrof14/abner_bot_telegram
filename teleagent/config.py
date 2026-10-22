"""Carregamento e validação da configuração (.env)."""
from __future__ import annotations

import glob
import os
import platform
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

# Ferramentas que o Claude Code pode usar sem confirmação. Edit/Write ficam
# restritos ao diretório do projeto (cwd) pelo padrão "./**".
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "LS", "TodoWrite", "Task",
    "Edit(./**)", "Write(./**)", "NotebookEdit(./**)",
    "Bash(ls:*)", "Bash(pwd)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    # Sem "find" (permite -delete) nem "echo" (permite redirecionar para fora do projeto).
    "Bash(grep:*)", "Bash(rg:*)", "Bash(mkdir:*)", "Bash(touch:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git branch)", "Bash(git branch --list:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git switch -c:*)", "Bash(git checkout -b:*)", "Bash(git stash list)",
    "Bash(npm install)", "Bash(npm ci)", "Bash(npm test:*)", "Bash(npm run:*)",
    "Bash(pnpm install)", "Bash(pnpm test:*)", "Bash(pnpm run:*)",
    "Bash(yarn install)", "Bash(yarn test:*)", "Bash(yarn run:*)",
    "Bash(pip install -r requirements.txt)", "Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)",
    "Bash(pytest:*)", "Bash(python -m venv .venv)", "Bash(python3 -m venv .venv)",
    "Bash(node --version)", "Bash(python --version)", "Bash(python3 --version)",
    "Bash(go build:*)", "Bash(go test:*)", "Bash(cargo build:*)", "Bash(cargo test:*)",
]

# Sempre negadas (defesa em profundidade, mesmo que alguma configuração do usuário
# as libere). Uma regra só é removida numa execução específica aprovada no Telegram.
DEFAULT_DENIED_TOOLS = [
    "Bash(rm:*)", "Bash(rmdir:*)", "Bash(unlink:*)", "Bash(shred:*)", "Bash(mv:*)",
    "Bash(chmod:*)", "Bash(chown:*)", "Bash(chgrp:*)", "Bash(sudo:*)", "Bash(su:*)",
    "Bash(dd:*)", "Bash(mkfs:*)",
    "Bash(git push:*)", "Bash(git reset --hard:*)", "Bash(git clean:*)",
    "Bash(git branch -D:*)", "Bash(git checkout --:*)", "Bash(git restore:*)",
    "Bash(git rebase:*)", "Bash(git filter-branch:*)",
    "Bash(npm publish:*)", "Bash(pnpm publish:*)", "Bash(yarn publish:*)",
    "Bash(twine upload:*)", "Bash(cargo publish:*)", "Bash(gh release:*)", "Bash(gh pr merge:*)",
    "Bash(docker push:*)", "Bash(vercel:*)", "Bash(netlify deploy:*)", "Bash(firebase deploy:*)",
]


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(";") if v.strip()]


def find_claude_bin(explicit: str | None = None) -> str | None:
    """Localiza o executável da CLI do Claude Code (não inventa API: usa a CLI instalada)."""
    if explicit:
        p = Path(explicit).expanduser()
        found = str(p) if p.exists() else shutil.which(explicit)
        if found:
            return found
        # Caminho configurado sumiu (ex.: extensão da IDE atualizada): segue para a detecção automática.
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    home = Path.home()
    exe = "claude.exe" if platform.system() == "Windows" else "claude"
    candidates = [
        home / ".local" / "bin" / exe,
        home / ".claude" / "local" / exe,
        home / ".npm-global" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Binário que acompanha a extensão do Claude Code em IDEs (VS Code e derivados).
    patterns = []
    for ide in (".vscode", ".vscode-insiders", ".cursor", ".windsurf", ".antigravity-ide"):
        patterns.append(str(home / ide / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / exe))
    found = sorted({m for pat in patterns for m in glob.glob(pat)}, key=os.path.getmtime, reverse=True)
    return found[0] if found else None


@dataclass
class Config:
    telegram_token: str
    owner_id: int
    projects_dirs: list[Path]
    sync_repo_url: str = ""
    sync_branch: str = "main"
    machine_name: str = field(default_factory=socket.gethostname)
    claude_bin: str | None = None
    claude_model: str = ""
    claude_max_budget_usd: float | None = None
    task_timeout: int = 3600
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    denied_tools: list[str] = field(default_factory=lambda: list(DEFAULT_DENIED_TOOLS))
    lease_stale_hours: float = 3.0
    heartbeat_minutes: float = 20.0
    log_level: str = "INFO"
    base_dir: Path = BASE_DIR

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def sync_dir(self) -> Path:
        return self.data_dir / "sync"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def local_state_file(self) -> Path:
        return self.data_dir / "local_state.json"


class ConfigError(Exception):
    pass


def load_config(env_file: Path = ENV_FILE, base_dir: Path = BASE_DIR) -> Config:
    if not env_file.exists():
        raise ConfigError(f"Arquivo {env_file} não encontrado. Rode o assistente: python -m teleagent setup")
    raw = {**dotenv_values(env_file)}
    # Variáveis de ambiente reais têm precedência (útil em testes/serviços).
    for key in list(raw) + ["TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"]:
        if os.environ.get(key):
            raw[key] = os.environ[key]

    errors = []
    token = (raw.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token or ":" not in token:
        errors.append("TELEGRAM_BOT_TOKEN ausente ou inválido")
    owner_raw = (raw.get("TELEGRAM_OWNER_ID") or "").strip()
    try:
        owner_id = int(owner_raw)
        if owner_id <= 0:
            raise ValueError
    except ValueError:
        owner_id = 0
        errors.append("TELEGRAM_OWNER_ID deve ser o ID numérico (positivo) do proprietário")

    dirs = [Path(p).expanduser().resolve() for p in _split(raw.get("PROJECTS_DIRS"))]
    if not dirs:
        errors.append("PROJECTS_DIRS vazio")
    for d in dirs:
        if not d.is_dir():
            errors.append(f"Diretório de projetos não existe: {d}")
        elif d == Path(d.anchor) or d == Path.home():
            errors.append(f"Diretório de projetos amplo demais (raiz ou home): {d}")
    if errors:
        raise ConfigError("; ".join(errors))

    def _num(key, default, cast):
        v = (raw.get(key) or "").strip()
        try:
            return cast(v) if v else default
        except ValueError:
            raise ConfigError(f"{key} inválido: {v!r}")

    allowed = _split(raw.get("CLAUDE_ALLOWED_TOOLS")) or list(DEFAULT_ALLOWED_TOOLS)
    denied = list(DEFAULT_DENIED_TOOLS) + _split(raw.get("CLAUDE_EXTRA_DENIED_TOOLS"))
    return Config(
        telegram_token=token,
        owner_id=owner_id,
        projects_dirs=dirs,
        sync_repo_url=(raw.get("SYNC_REPO_URL") or "").strip(),
        sync_branch=(raw.get("SYNC_BRANCH") or "main").strip(),
        machine_name=(raw.get("MACHINE_NAME") or "").strip() or socket.gethostname(),
        claude_bin=find_claude_bin((raw.get("CLAUDE_BIN") or "").strip() or None),
        claude_model=(raw.get("CLAUDE_MODEL") or "").strip(),
        claude_max_budget_usd=_num("CLAUDE_MAX_BUDGET_USD", None, float),
        task_timeout=_num("TASK_TIMEOUT_SECONDS", 3600, int),
        allowed_tools=allowed,
        denied_tools=denied,
        lease_stale_hours=_num("LEASE_STALE_HOURS", 3.0, float),
        heartbeat_minutes=_num("HEARTBEAT_MINUTES", 20.0, float),
        log_level=(raw.get("LOG_LEVEL") or "INFO").upper(),
        base_dir=base_dir,
    )
