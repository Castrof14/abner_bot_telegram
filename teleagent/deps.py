"""Detecção e instalação de dependências de projetos (sob demanda, com confirmação).

Os comandos são listas fixas de argumentos — nunca texto vindo do Telegram — e
rodam sem shell, dentro do diretório do projeto.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class InstallStep:
    manager: str
    argv: list[str]
    reason: str
    available: bool = True

    @property
    def display(self) -> str:
        return " ".join(self.argv)


def _venv_python(project: Path) -> Path | None:
    for name in (".venv", "venv"):
        exe = project / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if exe.exists():
            return exe
    return None


def _step(manager: str, argv: list[str], reason: str) -> InstallStep:
    return InstallStep(manager, argv, reason, available=shutil.which(argv[0]) is not None or Path(argv[0]).exists())


def detect(project: Path) -> list[InstallStep]:
    steps: list[InstallStep] = []
    f = lambda name: (project / name).exists()  # noqa: E731

    # JavaScript/TypeScript — o lockfile decide o gerenciador.
    if f("package.json"):
        if f("pnpm-lock.yaml"):
            steps.append(_step("pnpm", ["pnpm", "install"], "pnpm-lock.yaml"))
        elif f("yarn.lock"):
            steps.append(_step("yarn", ["yarn", "install"], "yarn.lock"))
        elif f("bun.lockb") or f("bun.lock"):
            steps.append(_step("bun", ["bun", "install"], "bun.lock"))
        else:
            steps.append(_step("npm", ["npm", "install"], "package-lock.json" if f("package-lock.json") else "package.json"))

    # Python
    if f("uv.lock"):
        steps.append(_step("uv", ["uv", "sync"], "uv.lock"))
    elif f("poetry.lock"):
        steps.append(_step("poetry", ["poetry", "install"], "poetry.lock"))
    elif f("pdm.lock"):
        steps.append(_step("pdm", ["pdm", "install"], "pdm.lock"))
    elif f("Pipfile"):
        steps.append(_step("pipenv", ["pipenv", "install"], "Pipfile"))
    elif f("requirements.txt") or f("pyproject.toml") or f("setup.py"):
        py = _venv_python(project)
        if py is None:
            steps.append(InstallStep("venv", [sys.executable, "-m", "venv", ".venv"], "cria ambiente virtual .venv"))
            py = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if f("requirements.txt"):
            steps.append(InstallStep("pip", [str(py), "-m", "pip", "install", "-r", "requirements.txt"], "requirements.txt"))
        else:
            steps.append(InstallStep("pip", [str(py), "-m", "pip", "install", "-e", "."], "pyproject.toml/setup.py"))

    # Outros ecossistemas
    if f("Gemfile"):
        steps.append(_step("bundler", ["bundle", "install"], "Gemfile"))
    if f("go.mod"):
        steps.append(_step("go", ["go", "mod", "download"], "go.mod"))
    if f("Cargo.toml"):
        steps.append(_step("cargo", ["cargo", "fetch"], "Cargo.toml"))
    if f("composer.json"):
        steps.append(_step("composer", ["composer", "install"], "composer.json"))
    return steps


def looks_uninstalled(project: Path) -> bool:
    if (project / "package.json").exists() and not (project / "node_modules").exists():
        return True
    if (project / "requirements.txt").exists() and _venv_python(project) is None:
        return True
    return False


async def run_steps(project: Path, steps: list[InstallStep], log_file: Path, timeout: float = 1800) -> list[tuple[InstallStep, int, str]]:
    """Executa os passos em sequência; para no primeiro erro. Retorna (passo, código, cauda da saída)."""
    results = []
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as logf:
        for step in steps:
            if not step.available:
                results.append((step, 127, f"'{step.argv[0]}' não está instalado nesta máquina"))
                break
            exe = shutil.which(step.argv[0]) or step.argv[0]
            logf.write(f"\n$ {step.display}\n")
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe, *step.argv[1:], cwd=str(project), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), timeout)
                code = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                out, code = f"tempo esgotado ({timeout}s)".encode(), 124
            except asyncio.CancelledError:
                proc.kill()
                logf.write("\n[cancelado pelo usuário]\n")
                raise
            except OSError as e:
                out, code = str(e).encode(), 126
            text = out.decode("utf-8", "replace")
            logf.write(text)
            results.append((step, code, text[-1500:]))
            if code != 0:
                break
    return results
