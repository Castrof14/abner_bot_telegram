"""Estado local desta máquina (NÃO sincronizado): projeto selecionado, sessões do
Claude Code nesta máquina e tarefa em andamento (para detectar interrupções)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        # Arquivo corrompido: preserva uma cópia em vez de sobrescrever silenciosamente.
        backup = path.with_suffix(path.suffix + f".corrupt-{datetime.now():%Y%m%d%H%M%S}")
        try:
            path.replace(backup)
        except OSError:
            pass
        return default


class LocalState:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = read_json(path, {})
        self.data.setdefault("sessions", {})

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self.data)

    @property
    def selected_project(self) -> str | None:
        return self.data.get("selected_project")

    @selected_project.setter
    def selected_project(self, name: str | None) -> None:
        self.data["selected_project"] = name
        self.save()

    def session_for(self, project: str) -> str | None:
        return self.data["sessions"].get(project)

    def set_session(self, project: str, session_id: str | None) -> None:
        if session_id:
            self.data["sessions"][project] = session_id
        else:
            self.data["sessions"].pop(project, None)
        self.save()

    @property
    def running_task(self) -> dict | None:
        return self.data.get("running_task")

    @running_task.setter
    def running_task(self, info: dict | None) -> None:
        self.data["running_task"] = info
        self.save()
