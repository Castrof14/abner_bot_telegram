"""Impede duas instâncias fazendo polling do mesmo bot.

Três camadas:
1. Trava local do SO (fcntl/msvcrt) — duas instâncias na MESMA máquina.
2. "Lease" em locks/instance.json no repositório de memória — entre máquinas.
   Ao iniciar, a máquina publica que é a dona; ao parar (/parar → desligar)
   libera. Um heartbeat periódico mantém a lease viva.
3. Detecção do erro 409 Conflict do Telegram (getUpdates simultâneo) —
   última barreira caso as anteriores falhem (ex.: sem rede para o Git).
"""
from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import atomic_write_json, now_iso, parse_iso, read_json


class InstanceLockError(Exception):
    pass


class LocalInstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip()
            fh.close()
            raise InstanceLockError(f"Já existe um TeleAgent rodando nesta máquina ({holder or 'pid desconhecido'}).")
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} host={socket.gethostname()} desde={now_iso()}")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


@dataclass
class LeaseCheck:
    can_start: bool
    message: str
    holder: dict | None = None


class Lease:
    """Lease entre máquinas, guardada no repositório de memória."""

    def __init__(self, sync_dir: Path, machine: str, stale_hours: float):
        self.path = sync_dir / "locks" / "instance.json"
        self.machine = machine
        self.instance_id = uuid.uuid4().hex[:12]
        self.stale = timedelta(hours=stale_hours)

    def read(self) -> dict | None:
        return read_json(self.path, None)

    def check(self, takeover: bool = False) -> LeaseCheck:
        cur = self.read()
        if not cur or cur.get("released"):
            return LeaseCheck(True, "Nenhuma outra máquina ativa.", cur)
        if cur.get("machine") == self.machine:
            return LeaseCheck(True, "A lease já pertence a esta máquina (reinício).", cur)
        hb = parse_iso(cur.get("heartbeat")) or parse_iso(cur.get("since"))
        age = datetime.now(timezone.utc) - hb if hb else None
        if takeover:
            return LeaseCheck(True, f"Assumindo à força a lease de '{cur.get('machine')}'.", cur)
        if age is not None and age > self.stale:
            return LeaseCheck(True, f"A lease de '{cur.get('machine')}' está inativa há "
                                    f"{age.total_seconds() / 3600:.1f} h; assumindo.", cur)
        return LeaseCheck(False, f"O bot está ativo em '{cur.get('machine')}' (último sinal: {cur.get('heartbeat')}). "
                                 "Pare-o lá com /parar → Desligar, ou inicie aqui com --takeover se aquela "
                                 "máquina estiver desligada.", cur)

    def owned_by_other(self) -> dict | None:
        cur = self.read()
        if cur and not cur.get("released") and cur.get("instance") != self.instance_id \
                and cur.get("machine") != self.machine:
            return cur
        return None

    def write(self, released: bool = False) -> None:
        cur = self.read() or {}
        data = {
            "machine": self.machine,
            "instance": self.instance_id,
            "since": cur.get("since") if cur.get("instance") == self.instance_id else now_iso(),
            "heartbeat": now_iso(),
            "released": released,
        }
        atomic_write_json(self.path, data)
