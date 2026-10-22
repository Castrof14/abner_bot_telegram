"""Validação de caminhos, redação de segredos e classificação de ações negadas."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# Letras (inclusive acentuadas), números, espaço, ponto, hífen e _; nunca barras.
PROJECT_NAME_RE = re.compile(r"^\w[\w. -]{0,99}$")

# (padrão, substituição)
SECRET_RULES = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED]"),                  # token de bot Telegram
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), "[REDACTED]"),                         # Anthropic
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[REDACTED]"),                             # OpenAI e similares
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),                      # GitHub
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),                        # GitLab
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),                    # Slack
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),                                # AWS
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED]"),                           # Google
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)\b(password|passwd|senha|secret|token|api[_-]?key)(\s*[=:]\s*)(?!\[REDACTED)['\"]?[^\s'\"]{6,}['\"]?"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),             # credenciais em URL
]
SECRET_PATTERNS = [rx for rx, _ in SECRET_RULES]

SENSITIVE_FILENAMES = re.compile(
    r"(^|/)(\.env(\..+)?|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|.*\.p12|.*\.pfx|credentials(\.json)?|\.npmrc|\.pypirc|\.netrc)$",
    re.IGNORECASE,
)


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    """Mascara segredos conhecidos antes de salvar em memória, logs ou Git."""
    if not text:
        return text
    for s in extra_secrets or []:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    for rx, repl in SECRET_RULES:
        text = rx.sub(repl, text)
    return text


def contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in SECRET_PATTERNS)


def strip_url_credentials(url: str) -> str:
    return re.sub(r"^(https?://)[^/@]+@", r"\1", url or "")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class PathError(Exception):
    pass


def _excluded(path: Path, exclude: list[Path]) -> bool:
    """Pasta protegida: é (ou contém) um caminho excluído, como a do próprio TeleAgent."""
    return any(is_within(path, ex) or is_within(ex, path) for ex in exclude)


def resolve_project(name: str, roots: list[Path], exclude: list[Path] | None = None) -> Path:
    """Converte um nome de projeto num caminho dentro de um diretório autorizado."""
    raw = name or ""
    name = raw.strip()
    if not PROJECT_NAME_RE.match(name) or name in (".", "..") or ".." in name or not PROJECT_NAME_RE.match(raw):
        raise PathError(f"Nome de projeto inválido: {name!r}")
    for root in roots:
        # Pastas reais podem terminar com espaço ("site lucas "): tenta o nome exato primeiro.
        candidate = root / raw if (root / raw).is_dir() else root / name
        if candidate.is_dir():
            resolved = candidate.resolve()
            if not is_within(resolved, root) or resolved == root.resolve():
                raise PathError(f"Projeto fora dos diretórios autorizados: {name!r}")
            if _excluded(resolved, exclude or []):
                raise PathError(f"Pasta protegida, não pode ser usada como projeto: {name!r}")
            return resolved
    raise PathError(f"Projeto não encontrado: {name!r}")


def list_projects(roots: list[Path], exclude: list[Path] | None = None) -> list[tuple[str, Path]]:
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if p.is_dir() and not p.name.startswith(".") and PROJECT_NAME_RE.match(p.name) and is_within(p, root) \
                    and not _excluded(p, exclude or []):
                out.append((p.name, p.resolve()))
    return out


# ---------------------------------------------------------------- ações negadas

CATEGORIES = [
    ("excluir arquivos", re.compile(r"(^|[\s;&|(])(rm|rmdir|unlink|shred|del|erase|Remove-Item)\b|-delete\b|os\.remove|shutil\.rmtree|unlinkSync|rmSync")),
    ("mover/sobrescrever", re.compile(r"(^|[\s;&|(])(mv|move|Move-Item)\b")),
    ("alterar permissões", re.compile(r"(^|[\s;&|(])(chmod|chown|chgrp|icacls|takeown|attrib)\b")),
    ("publicar código", re.compile(r"git\s+push|\bpublish\b|twine\s+upload|docker\s+push|\bdeploy\b|gh\s+(release|pr\s+merge)|vercel|netlify")),
    ("reescrever histórico Git", re.compile(r"git\s+(reset\s+--hard|clean|rebase|filter-branch|branch\s+-D|checkout\s+--|restore)")),
]

# Nunca oferecidos para aprovação pelo Telegram.
BLOCKED = [
    re.compile(r"(^|[\s;&|(])sudo\b"),
    re.compile(r"(^|[\s;&|(])su\s"),
    re.compile(r"\bmkfs\b|\bdd\b.*\bof=/dev/|\bformat\s+[a-zA-Z]:"),
    re.compile(r"(^|[\s;&|(])(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r":\(\)\s*\{"),                                      # fork bomb
    re.compile(r"(curl|wget|iwr|Invoke-WebRequest)[^|]*\|\s*(sh|bash|zsh|iex|python)"),
    re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*(/|~|\$HOME|\*|\.\.?/?)(\s|$)"),
]


@dataclass
class DenialReview:
    tool: str
    summary: str          # texto mostrado ao usuário
    rule: str | None      # regra --allowedTools para liberar exatamente esta ação
    category: str         # motivo da confirmação
    offer: bool           # pode ser aprovada pelo Telegram?
    reason: str = ""      # por que não pode (se offer=False)


def _paths_outside(command: str, project: Path) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    bad = []
    for t in tokens:
        if t.startswith(("/", "~", "..")) or re.match(r"^[A-Za-z]:[\\/]", t):
            p = Path(t).expanduser()
            if not p.is_absolute():
                p = project / p
            if not is_within(p, project) and not t.startswith("/dev/null"):
                bad.append(t)
    return bad


def review_denial(denial: dict, project: Path) -> DenialReview:
    tool = denial.get("tool_name", "?")
    inp = denial.get("tool_input") or {}
    if tool in ("Bash", "PowerShell"):
        cmd = (inp.get("command") or "").strip()
        summary = cmd if len(cmd) <= 300 else cmd[:300] + "…"
        category = next((name for name, rx in CATEGORIES if rx.search(cmd)), "comando não pré-aprovado")
        if not cmd or "\n" in cmd:
            return DenialReview(tool, summary or "(vazio)", None, category, False, "comando multilinha ou vazio")
        for rx in BLOCKED:
            if rx.search(cmd):
                return DenialReview(tool, summary, None, category, False, "comando bloqueado pela política do TeleAgent")
        if category in ("excluir arquivos", "mover/sobrescrever", "alterar permissões"):
            outside = _paths_outside(cmd, project)
            if outside:
                return DenialReview(tool, summary, None, category, False,
                                    f"afeta caminhos fora do projeto: {', '.join(outside[:3])}")
        return DenialReview(tool, summary, f"{tool}({cmd})", category, True)
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit", "Read"):
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        p = Path(fp)
        if fp and not p.is_absolute():
            p = project / p
        if not fp or not is_within(p, project):
            return DenialReview(tool, fp or "(sem caminho)", None, "fora do projeto", False,
                                "operações de arquivo são restritas ao diretório do projeto")
        return DenialReview(tool, fp, f"{tool}({fp})", "arquivo", True)
    if tool in ("WebFetch", "WebSearch"):
        target = inp.get("url") or inp.get("query") or ""
        return DenialReview(tool, str(target)[:200], tool, "acesso à internet", True)
    return DenialReview(tool, str(inp)[:200], tool, "ferramenta não pré-aprovada", True)


def deny_rules_for_approved(denied: list[str], approved_rules: list[str]) -> list[str]:
    """Remove das regras de negação apenas as que bloqueiam os comandos aprovados."""
    approved_cmds = []
    for r in approved_rules:
        m = re.match(r"^(Bash|PowerShell)\((.*)\)$", r, re.S)
        if m:
            # Comandos compostos: cada subcomando pode esbarrar numa regra de negação.
            approved_cmds += [c.strip() for c in re.split(r"&&|\|\||;|\|", m.group(2)) if c.strip()]
    keep = []
    for rule in denied:
        m = re.match(r"^Bash\((.*?)(:\*)?\)$", rule)
        if m and any(cmd == m.group(1) or cmd.startswith(m.group(1) + " ") for cmd in approved_cmds):
            continue
        keep.append(rule)
    return keep
