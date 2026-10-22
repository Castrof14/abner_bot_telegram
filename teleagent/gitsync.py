"""Sincronização Git do repositório de memória (e operações Git seguras em projetos).

Garantias:
- Alterações locais são SEMPRE commitadas antes de buscar as remotas.
- Mescla com `git merge` (nunca reset/force-push). Conflitos em state.json são
  resolvidos semanticamente (união por ID); MEMORY.md é regenerado.
- Qualquer outro conflito aborta a mescla, mantém o commit local intacto, não
  faz push e avisa o usuário.
- Antes de cada commit, o conteúdo preparado é verificado contra padrões de segredo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .memory import merge_states, render_markdown
from .security import SENSITIVE_FILENAMES, contains_secret
from .storage import atomic_write_json, atomic_write_text

log = logging.getLogger(__name__)

SYNC_GITIGNORE = """\
# Repositório de memória do TeleAgent: somente memória e trava de instância.
*
!*/
!.gitignore
!.gitattributes
!README.md
!memory/**
!locks/**
*.tmp
*.corrupt-*
"""

SYNC_README = """\
# Memória do TeleAgent

Repositório privado gerenciado pelo TeleAgent. Contém apenas:

- `memory/<projeto>/state.json` — memória estruturada (fonte da verdade)
- `memory/<projeto>/MEMORY.md` — visão legível, regenerada automaticamente
- `locks/instance.json` — qual computador está executando o bot

Não coloque segredos aqui. Sessões e arquivos internos do Claude Code não são sincronizados.
"""

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "LC_ALL": "C",
           "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")}


class SecretBlocked(RuntimeError):
    pass


@dataclass
class GitResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass
class SyncReport:
    ok: bool
    message: str
    pushed: bool = False
    pulled: bool = False
    conflicts: list[str] = field(default_factory=list)
    changed_projects: list[str] = field(default_factory=list)


async def git(cwd: Path, *args: str, timeout: float = 120, identity: tuple[str, str] | None = None) -> GitResult:
    pre = []
    if identity:
        pre = ["-c", f"user.name={identity[0]}", "-c", f"user.email={identity[1]}"]
    env = {**os.environ, **GIT_ENV}
    proc = await asyncio.create_subprocess_exec(
        "git", *pre, *args, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return GitResult(124, "", f"git {' '.join(args)}: tempo esgotado ({timeout}s)")
    return GitResult(proc.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip())


class GitSync:
    def __init__(self, repo_dir: Path, remote_url: str, branch: str, machine: str):
        self.dir = repo_dir
        self.url = remote_url
        self.branch = branch
        self.machine = machine
        self.identity = (f"TeleAgent ({machine})", f"teleagent@{_safe_host(machine)}.local")
        self._lock = asyncio.Lock()
        self.last_report: SyncReport | None = None

    async def _git(self, *args, timeout: float = 120) -> GitResult:
        return await git(self.dir, *args, timeout=timeout, identity=self.identity)

    @property
    def has_remote(self) -> bool:
        return bool(self.url)

    async def ensure_repo(self) -> SyncReport:
        if not (self.dir / ".git").exists():
            self.dir.parent.mkdir(parents=True, exist_ok=True)
            if self.dir.exists() and any(self.dir.iterdir()):
                return SyncReport(False, f"{self.dir} existe, não está vazio e não é um repositório Git.")
            if self.url:
                r = await git(self.dir.parent, "clone", self.url, self.dir.name, timeout=300)
                if not r.ok:
                    return SyncReport(False, f"Falha ao clonar {self.url}: {r.err[-500:]}")
            else:
                self.dir.mkdir(parents=True, exist_ok=True)
                r = await self._git("init")
                if not r.ok:
                    return SyncReport(False, f"git init falhou: {r.err}")
            # Repositório recém-clonado vazio ou novo: fixa o branch configurado.
            head = await self._git("rev-parse", "--verify", "HEAD")
            if not head.ok:
                await self._git("symbolic-ref", "HEAD", f"refs/heads/{self.branch}")
        elif self.url:
            cur = await self._git("remote", "get-url", "origin")
            if not cur.ok:
                await self._git("remote", "add", "origin", self.url)
            elif cur.out != self.url:
                return SyncReport(False, f"O repositório em {self.dir} aponta para {cur.out}, "
                                         f"mas SYNC_REPO_URL é {self.url}. Corrija manualmente.")
        changed = False
        for name, content in ((".gitignore", SYNC_GITIGNORE), ("README.md", SYNC_README)):
            p = self.dir / name
            if not p.exists() or (name == ".gitignore" and p.read_text(encoding="utf-8") != content):
                atomic_write_text(p, content)
                changed = True
        (self.dir / "memory").mkdir(exist_ok=True)
        if changed:
            await self.commit("TeleAgent: inicializa repositório de memória")
        return SyncReport(True, "Repositório de memória pronto.")

    async def _staged_secret_check(self) -> list[str]:
        names = await self._git("diff", "--cached", "--name-only")
        problems = [n for n in names.out.splitlines() if n and SENSITIVE_FILENAMES.search(n)]
        diff = await self._git("diff", "--cached", "-U0", "--no-color")
        added = "\n".join(l[1:] for l in diff.out.splitlines() if l.startswith("+") and not l.startswith("+++"))
        if contains_secret(added):
            problems.append("conteúdo com padrão de segredo")
        return problems

    async def commit(self, message: str) -> GitResult | None:
        await self._git("add", "-A")
        staged = await self._git("diff", "--cached", "--quiet")
        if staged.ok:
            return None  # nada a commitar
        problems = await self._staged_secret_check()
        if problems:
            await self._git("reset", "-q")  # apenas tira do índice; arquivos permanecem
            raise SecretBlocked("Commit bloqueado: possível segredo detectado (" + ", ".join(problems) + ")")
        return await self._git("commit", "-q", "-m", message)

    async def sync(self, message: str = "TeleAgent: atualiza memória") -> SyncReport:
        async with self._lock:
            try:
                report = await self._sync(message)
            except SecretBlocked as e:
                log.warning("%s", e)
                report = SyncReport(False, str(e))
            except Exception as e:  # noqa: BLE001 — reportado ao usuário
                log.exception("Erro na sincronização")
                report = SyncReport(False, f"Erro na sincronização: {e}")
            self.last_report = report
            return report

    async def _sync(self, message: str) -> SyncReport:
        await self.commit(f"{message} [{self.machine}]")
        if not self.has_remote:
            return SyncReport(True, "Memória salva localmente (sem repositório remoto configurado).")
        fetch = await self._git("fetch", "origin", timeout=180)
        if not fetch.ok:
            return SyncReport(False, f"Falha no fetch (sem rede ou sem acesso?): {fetch.err[-400:]}")
        remote_ref = f"origin/{self.branch}"
        has_remote_branch = (await self._git("rev-parse", "--verify", "-q", remote_ref)).ok
        has_local = (await self._git("rev-parse", "--verify", "-q", "HEAD")).ok
        pulled = False
        changed: list[str] = []
        if has_remote_branch:
            if not has_local:
                r = await self._git("checkout", "-q", "-B", self.branch, remote_ref)
                if not r.ok:
                    return SyncReport(False, f"Falha ao obter {remote_ref}: {r.err}")
                return SyncReport(True, "Memória baixada do repositório remoto.", pulled=True)
            before = (await self._git("rev-parse", "HEAD")).out
            merge = await self._git("merge", "--no-edit", "-m",
                                    f"TeleAgent: mescla memória [{self.machine}]", remote_ref)
            if not merge.ok:
                resolved, conflicts = await self._resolve_conflicts()
                if conflicts:
                    await self._git("merge", "--abort")
                    return SyncReport(False, "Conflito que não pode ser resolvido automaticamente em: "
                                      + ", ".join(conflicts) + ". Suas alterações locais estão preservadas "
                                      "em commits locais; nada foi enviado. Resolva em " + str(self.dir),
                                      conflicts=conflicts)
                r = await self._git("commit", "-q", "--no-edit")
                if not r.ok:
                    await self._git("merge", "--abort")
                    return SyncReport(False, f"Falha ao concluir a mescla: {r.err}")
                changed = resolved
            after = (await self._git("rev-parse", "HEAD")).out
            pulled = before != after
            if pulled:
                diff = await self._git("diff", "--name-only", before, after)
                changed = sorted({p.split("/")[1] for p in diff.out.splitlines()
                                  if p.startswith("memory/") and p.count("/") >= 2} | set(changed))
        ahead = await self._git("rev-list", "--count", f"{remote_ref}..HEAD") if has_remote_branch else None
        if has_remote_branch and ahead and ahead.out == "0":
            return SyncReport(True, "Memória sincronizada.", pulled=pulled, changed_projects=changed)
        push = await self._git("push", "-u", "origin", f"HEAD:refs/heads/{self.branch}", timeout=180)
        if not push.ok:
            if "rejected" in push.err or "fetch first" in push.err or "non-fast-forward" in push.err:
                return SyncReport(False, "O remoto mudou durante a sincronização; tente /sync novamente. "
                                  "Nada foi sobrescrito.", pulled=pulled, changed_projects=changed)
            return SyncReport(False, f"Falha no push: {push.err[-400:]}", pulled=pulled, changed_projects=changed)
        return SyncReport(True, "Memória sincronizada e enviada.", pushed=True, pulled=pulled,
                          changed_projects=changed)

    async def _resolve_conflicts(self) -> tuple[list[str], list[str]]:
        """Resolve conflitos de arquivos gerenciados. Retorna (projetos resolvidos, conflitos restantes)."""
        files = (await self._git("diff", "--name-only", "--diff-filter=U")).out.splitlines()
        unresolved, projects, rerender = [], [], []
        for f in files:
            if f.startswith("memory/") and f.endswith("/state.json"):
                ours = await self._git("show", f":2:{f}")
                theirs = await self._git("show", f":3:{f}")
                try:
                    a = json.loads(ours.out) if ours.ok else {}
                    b = json.loads(theirs.out) if theirs.ok else {}
                except json.JSONDecodeError:
                    unresolved.append(f)
                    continue
                atomic_write_json(self.dir / f, merge_states(a, b))
                await self._git("add", f)
                projects.append(f.split("/")[1])
                rerender.append(Path(f).parent)
            elif f.startswith("memory/") and f.endswith("/MEMORY.md"):
                rerender.append(Path(f).parent)
            elif f == "locks/instance.json":
                ours = await self._git("show", f":2:{f}")
                theirs = await self._git("show", f":3:{f}")
                try:
                    a, b = json.loads(ours.out), json.loads(theirs.out)
                    pick = a if (a.get("heartbeat") or "") >= (b.get("heartbeat") or "") else b
                except (json.JSONDecodeError, AttributeError):
                    unresolved.append(f)
                    continue
                atomic_write_json(self.dir / f, pick)
                await self._git("add", f)
            elif f in (".gitignore", "README.md"):
                await self._git("checkout", "--ours", "--", f)
                await self._git("add", f)
            else:
                unresolved.append(f)
        for d in {*rerender}:
            state_file = self.dir / d / "state.json"
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                atomic_write_text(self.dir / d / "MEMORY.md", render_markdown(state))
                await self._git("add", str(d / "MEMORY.md"))
            elif (d / "MEMORY.md").as_posix() in files:
                unresolved.append((d / "MEMORY.md").as_posix())
        return projects, unresolved

    async def status_line(self) -> str:
        if not (self.dir / ".git").exists():
            return "não inicializado"
        if not self.has_remote:
            return "somente local"
        ahead = await self._git("rev-list", "--count", f"origin/{self.branch}..HEAD")
        behind = await self._git("rev-list", "--count", f"HEAD..origin/{self.branch}")
        if not ahead.ok:
            return "remoto ainda sem commits"
        return f"{ahead.out} commit(s) a enviar, {behind.out} a receber (desde o último fetch)"


def _safe_host(name: str) -> str:
    return "".join(c if c.isalnum() or c == "-" else "-" for c in name.lower()) or "host"


# ---------------------------------------------------------------- Git em projetos

async def project_git_info(path: Path) -> dict:
    info = {"is_repo": (path / ".git").exists()}
    if not info["is_repo"]:
        return info
    branch = await git(path, "rev-parse", "--abbrev-ref", "HEAD")
    status = await git(path, "status", "--porcelain")
    remote = await git(path, "remote", "get-url", "origin")
    upstream = await git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    info.update(branch=branch.out, dirty=bool(status.out.strip()), changes=status.out.splitlines(),
                remote=remote.out if remote.ok else "", upstream=upstream.out if upstream.ok else "")
    return info


async def project_safe_pull(path: Path) -> str:
    """Atualiza o projeto sem risco: só fast-forward e só com a árvore limpa."""
    info = await project_git_info(path)
    if not info["is_repo"]:
        return "não é um repositório Git (nada a atualizar)"
    if not info.get("upstream"):
        return f"branch {info.get('branch')} sem upstream (nada a atualizar)"
    if info.get("dirty"):
        return "há alterações locais não commitadas — pull ignorado para não sobrescrever nada"
    fetch = await git(path, "fetch", "--quiet", timeout=180)
    if not fetch.ok:
        return f"fetch falhou: {fetch.err[-300:]}"
    r = await git(path, "merge", "--ff-only", "@{u}")
    if r.ok:
        return "atualizado (fast-forward)" if "Already up to date" not in r.out else "já estava atualizado"
    return "o branch local divergiu do remoto — nada foi alterado; resolva manualmente ou peça ao agente"
