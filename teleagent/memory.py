"""Memória persistente por projeto.

Cada projeto tem, no repositório de sincronização:
  memory/<projeto>/state.json  — fonte da verdade, estruturada e mesclável
  memory/<projeto>/MEMORY.md   — visão legível, sempre regenerada a partir do JSON

Itens têm ID e carimbo de tempo, de modo que a mescla entre computadores é uma
união por ID (vence a edição mais recente), sem perda silenciosa de dados.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .security import redact, strip_url_credentials
from .storage import atomic_write_json, atomic_write_text, now_iso, read_json

KINDS = {
    "objective": "🎯 Objetivos",
    "decision": "🧭 Decisões",
    "pending": "📌 Pendências",
    "next": "➡️ Próximos passos",
    "done": "✅ Concluído",
    "note": "📝 Notas",
}
MAX_TASKS = 200
MAX_DONE = 300

MEMORY_BLOCK_RE = re.compile(r"<teleagent-memory>\s*(.*?)\s*</teleagent-memory>", re.S)

MEMORY_INSTRUCTIONS = """\
Ao terminar, inclua no FINAL da resposta um bloco exatamente neste formato (JSON válido),
para atualizar a memória persistente do projeto. Não coloque segredos, tokens ou senhas nele.
<teleagent-memory>
{"summary": "resumo curto do que foi feito",
 "objectives": ["novos objetivos do projeto, se surgirem"],
 "decisions": ["decisões técnicas tomadas nesta tarefa"],
 "done": ["itens concluídos nesta tarefa"],
 "pending": ["lista COMPLETA e atual de pendências em aberto"],
 "next_steps": ["próximos passos recomendados, em ordem"]}
</teleagent-memory>"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def safe_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return slug or "projeto"


def empty_state(project: str) -> dict:
    return {"project": project, "version": 1, "updated": now_iso(), "repo_url": "", "items": {}, "tasks": {}}


def extract_memory_block(text: str) -> tuple[str, dict | None]:
    """Retorna (texto sem o bloco, dados do bloco ou None)."""
    matches = list(MEMORY_BLOCK_RE.finditer(text or ""))
    if not matches:
        return text, None
    m = matches[-1]
    raw = m.group(1).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = None
    except json.JSONDecodeError:
        data = None
    clean = (text[: m.start()] + text[m.end():]).strip()
    return clean, data


# ---------------------------------------------------------------- mescla

def _newer(a: dict, b: dict) -> dict:
    return a if (a.get("updated") or "") >= (b.get("updated") or "") else b


def merge_states(ours: dict, theirs: dict) -> dict:
    """União por ID; em conflito do mesmo item vence o 'updated' mais recente."""
    merged = empty_state(ours.get("project") or theirs.get("project") or "")
    merged["repo_url"] = _newer(
        {"updated": ours.get("updated"), "v": ours.get("repo_url", "")},
        {"updated": theirs.get("updated"), "v": theirs.get("repo_url", "")},
    )["v"] or ours.get("repo_url") or theirs.get("repo_url", "")
    for key in ("items", "tasks"):
        a, b = ours.get(key) or {}, theirs.get(key) or {}
        out = {}
        for k in set(a) | set(b):
            if k in a and k in b:
                out[k] = _newer(a[k], b[k])
            else:
                out[k] = a.get(k) or b.get(k)
        merged[key] = out
    merged["updated"] = max(ours.get("updated") or "", theirs.get("updated") or "") or now_iso()
    _trim(merged)
    return merged


def _trim(state: dict) -> None:
    tasks = state["tasks"]
    if len(tasks) > MAX_TASKS:
        keep = sorted(tasks.values(), key=lambda t: t.get("started") or "", reverse=True)[:MAX_TASKS]
        state["tasks"] = {t["id"]: t for t in keep}
    done = [i for i in state["items"].values() if i.get("kind") == "done"]
    if len(done) > MAX_DONE:
        drop = sorted(done, key=lambda i: i.get("created") or "")[: len(done) - MAX_DONE]
        for i in drop:
            state["items"].pop(i["id"], None)


# ---------------------------------------------------------------- loja

class MemoryStore:
    def __init__(self, root: Path, machine: str, secrets: list[str] | None = None):
        self.root = root / "memory"
        self.machine = machine
        self.secrets = secrets or []

    def _dir(self, project: str) -> Path:
        return self.root / safe_slug(project)

    def state_path(self, project: str) -> Path:
        return self._dir(project) / "state.json"

    def markdown_path(self, project: str) -> Path:
        return self._dir(project) / "MEMORY.md"

    def known_projects(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir()):
            if (p / "state.json").exists():
                out.append(read_json(p / "state.json", {}).get("project") or p.name)
        return out

    def load(self, project: str) -> dict:
        state = read_json(self.state_path(project), None) or empty_state(project)
        state.setdefault("items", {})
        state.setdefault("tasks", {})
        return state

    def save(self, project: str, state: dict) -> None:
        state["updated"] = now_iso()
        _trim(state)
        atomic_write_json(self.state_path(project), state)
        atomic_write_text(self.markdown_path(project), render_markdown(state))

    def rerender(self, project_dir: Path) -> None:
        state = read_json(project_dir / "state.json", None)
        if state:
            atomic_write_text(project_dir / "MEMORY.md", render_markdown(state))

    # ---- edição

    def _clean(self, text: str) -> str:
        return redact(str(text).strip(), self.secrets)[:2000]

    def add_item(self, state: dict, kind: str, text: str) -> dict | None:
        text = self._clean(text)
        if not text or kind not in KINDS:
            return None
        for item in state["items"].values():
            if item["kind"] == kind and item.get("status") == "open" and _norm(item["text"]) == _norm(text):
                return item
        ts = now_iso()
        item = {"id": uuid.uuid4().hex[:12], "kind": kind, "text": text, "status": "open",
                "created": ts, "updated": ts, "host": self.machine}
        state["items"][item["id"]] = item
        return item

    def close_item(self, state: dict, item: dict, status: str = "closed") -> None:
        item["status"] = status
        item["updated"] = now_iso()
        item["host"] = self.machine

    def open_items(self, state: dict, kind: str) -> list[dict]:
        items = [i for i in state["items"].values() if i["kind"] == kind and i.get("status") == "open"]
        return sorted(items, key=lambda i: i.get("created") or "")

    def _replace_open(self, state: dict, kind: str, texts: list[str]) -> None:
        wanted = {_norm(self._clean(t)) for t in texts if str(t).strip()}
        for item in self.open_items(state, kind):
            if _norm(item["text"]) not in wanted:
                self.close_item(state, item, "resolved")
        for t in texts:
            self.add_item(state, kind, t)

    def set_repo_url(self, state: dict, url: str) -> None:
        state["repo_url"] = strip_url_credentials(url or "")

    def start_task(self, state: dict, prompt: str, kind: str = "task") -> dict:
        task = {"id": uuid.uuid4().hex[:12], "prompt": self._clean(prompt)[:1000], "kind": kind,
                "status": "running", "summary": "", "host": self.machine,
                "started": now_iso(), "finished": None, "cost_usd": None, "updated": now_iso()}
        state["tasks"][task["id"]] = task
        return task

    def finish_task(self, state: dict, task_id: str, status: str, result_text: str,
                    block: dict | None, cost: float | None = None) -> dict:
        task = state["tasks"].get(task_id) or {"id": task_id, "prompt": "", "host": self.machine,
                                                "started": now_iso()}
        task.update(status=status, finished=now_iso(), updated=now_iso(), cost_usd=cost)
        if block:
            task["summary"] = self._clean(block.get("summary") or "")[:1500]
            for kind, key in (("objective", "objectives"), ("decision", "decisions"), ("done", "done")):
                for t in _as_list(block.get(key)):
                    item = self.add_item(state, kind, t)
                    if item and kind == "done":
                        self._resolve_matching_pending(state, item["text"])
            if "pending" in block:
                self._replace_open(state, "pending", _as_list(block.get("pending")))
            if "next_steps" in block:
                self._replace_open(state, "next", _as_list(block.get("next_steps")))
        else:
            task["summary"] = self._clean(result_text)[:800]
        if status in ("interrupted", "error", "timeout", "cancelled"):
            self.add_item(state, "pending", f"Retomar tarefa interrompida ({status}): {task.get('prompt', '')[:300]}")
        elif status == "done":
            for item in self.open_items(state, "pending"):
                if item["text"].startswith("Retomar tarefa interrompida") and task.get("prompt", "")[:300] in item["text"]:
                    self.close_item(state, item, "resolved")
        state["tasks"][task_id] = task
        return task

    def _resolve_matching_pending(self, state: dict, done_text: str) -> None:
        for item in self.open_items(state, "pending"):
            if _norm(item["text"]) == _norm(done_text):
                self.close_item(state, item, "resolved")

    def recent_tasks(self, state: dict, n: int = 5) -> list[dict]:
        return sorted(state["tasks"].values(), key=lambda t: t.get("started") or "", reverse=True)[:n]


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


# ---------------------------------------------------------------- renderização

def render_markdown(state: dict) -> str:
    items = state.get("items") or {}
    lines = [f"# Memória — {state.get('project', '')}", "",
             f"_Atualizado em {state.get('updated', '')}. Gerado pelo TeleAgent a partir de state.json — "
             "edite pelo bot (/memoria) ou pelo JSON._", ""]
    if state.get("repo_url"):
        lines += [f"Repositório do código: `{state['repo_url']}`", ""]
    for kind, title in KINDS.items():
        if kind == "done":
            sel = sorted((i for i in items.values() if i["kind"] == "done"),
                         key=lambda i: i.get("created") or "", reverse=True)[:30]
        else:
            sel = sorted((i for i in items.values() if i["kind"] == kind and i.get("status") == "open"),
                         key=lambda i: i.get("created") or "")
        if not sel:
            continue
        lines.append(f"## {title}")
        for i in sel:
            lines.append(f"- {i['text']}  _(⟨{i.get('host', '?')}⟩ {i.get('created', '')[:10]})_")
        lines.append("")
    tasks = sorted((state.get("tasks") or {}).values(), key=lambda t: t.get("started") or "", reverse=True)[:15]
    if tasks:
        lines.append("## 🗂️ Histórico de tarefas")
        for t in tasks:
            lines.append(f"- **{t.get('status')}** {t.get('started', '')[:16]} ⟨{t.get('host', '?')}⟩ — "
                         f"{t.get('prompt', '')[:160]}")
            if t.get("summary"):
                lines.append(f"  - {t['summary'][:400]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_context(state: dict) -> str:
    """Contexto compacto injetado no prompt do Claude Code."""
    items = state.get("items") or {}

    def open_of(kind, limit=None):
        sel = sorted((i for i in items.values() if i["kind"] == kind and i.get("status") == "open"),
                     key=lambda i: i.get("created") or "")
        return sel[-limit:] if limit else sel

    parts = []
    for kind, title, limit in (("objective", "Objetivos", None), ("decision", "Decisões", 15),
                               ("pending", "Pendências", None), ("next", "Próximos passos", None),
                               ("note", "Notas", 10)):
        sel = open_of(kind, limit)
        if sel:
            parts.append(f"{title}:\n" + "\n".join(f"- {i['text']}" for i in sel))
    done = sorted((i for i in items.values() if i["kind"] == "done"),
                  key=lambda i: i.get("created") or "")[-10:]
    if done:
        parts.append("Concluído recentemente:\n" + "\n".join(f"- {i['text']}" for i in done))
    tasks = sorted((state.get("tasks") or {}).values(), key=lambda t: t.get("started") or "", reverse=True)[:5]
    if tasks:
        parts.append("Últimas tarefas:\n" + "\n".join(
            f"- [{t.get('status')}] ({t.get('host')}) {t.get('prompt', '')[:150]} → {t.get('summary', '')[:200]}"
            for t in tasks))
    return "\n\n".join(parts) if parts else "(sem memória registrada ainda)"
