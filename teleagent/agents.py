"""Agentes de IA configuráveis pelo Telegram.

Um agente é um perfil com nome, motor ("claude" ou "opencode"), modelo e instruções
próprias (personalidade/papel). O agente selecionado para um projeto define qual CLI
e qual modelo executam as tarefas e acrescenta as instruções ao prompt.

Os agentes ficam em data/agents.json (local a esta máquina, não sincronizado): o motor
e os modelos disponíveis dependem do que está instalado em cada computador.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .storage import atomic_write_json, now_iso, read_json

AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RUNNERS = ("claude", "opencode")
MAX_INSTRUCTIONS = 4000

# Aliases aceitos por `claude --model`. "" = padrão da CLI (ou CLAUDE_MODEL do .env).
CLAUDE_MODELS = [
    ("", "Padrão"),
    ("sonnet", "Sonnet"),
    ("opus", "Opus"),
    ("haiku", "Haiku"),
]


@dataclass
class Agent:
    name: str
    runner: str = "claude"
    model: str = ""
    instructions: str = ""
    created: str = field(default_factory=now_iso)
    updated: str = field(default_factory=now_iso)

    def describe(self) -> str:
        lines = [f"🤖 Agente: {self.name}",
                 f"   motor: {runner_label(self.runner)}",
                 f"   modelo: {self.model or 'padrão'}"]
        if self.instructions:
            lines.append(f"   instruções: {self.instructions[:300]}{'…' if len(self.instructions) > 300 else ''}")
        else:
            lines.append("   instruções: (nenhuma)")
        return "\n".join(lines)


def runner_label(runner: str) -> str:
    return {"claude": "Claude Code", "opencode": "OpenCode"}.get(runner, runner)


def is_free_model(model: str) -> bool:
    """Modelos gratuitos do OpenCode Zen (provider `opencode/`, sufixo `-free` ou o big-pickle)."""
    return model.startswith("opencode/") and (model.endswith("-free") or model == "opencode/big-pickle")


class AgentStore:
    def __init__(self, path: Path):
        self.path = path
        self._agents: dict[str, Agent] = {}
        for name, raw in (read_json(path, {}).get("agents") or {}).items():
            try:
                self._agents[name] = Agent(**{k: v for k, v in raw.items() if k in Agent.__dataclass_fields__})
            except TypeError:
                continue

    def save(self) -> None:
        atomic_write_json(self.path, {"agents": {n: asdict(a) for n, a in self._agents.items()}})

    def list(self) -> list[Agent]:
        return sorted(self._agents.values(), key=lambda a: a.name.lower())

    def get(self, name: str | None) -> Agent | None:
        return self._agents.get(name) if name else None

    def put(self, agent: Agent) -> None:
        if not AGENT_NAME_RE.match(agent.name):
            raise ValueError("Nome inválido: use 1 a 32 letras, números, - ou _.")
        if agent.runner not in RUNNERS:
            raise ValueError(f"Motor inválido: {agent.runner}")
        agent.instructions = agent.instructions[:MAX_INSTRUCTIONS]
        agent.updated = now_iso()
        self._agents[agent.name] = agent
        self.save()

    def delete(self, name: str) -> bool:
        if self._agents.pop(name, None) is None:
            return False
        self.save()
        return True


async def opencode_models(opencode_bin: str | None, timeout: float = 30) -> list[str]:
    """Lista os modelos que o opencode CLI conhece (`opencode models`), gratuitos primeiro."""
    if not opencode_bin:
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            opencode_bin, "models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        return []
    models = [l.strip() for l in out.decode("utf-8", "replace").splitlines() if "/" in l and " " not in l.strip()]
    return sorted(dict.fromkeys(models), key=lambda m: (not is_free_model(m), m))
