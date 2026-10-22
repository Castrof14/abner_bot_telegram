"""Logs rotativos com redação de segredos (o token do bot aparece em URLs da API)."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .security import redact


class RedactingFormatter(logging.Formatter):
    """Mascara segredos na saída final, incluindo tracebacks."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def setup_logging(log_dir: Path, level: str = "INFO", secrets: list[str] | None = None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", secrets or [])

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "teleagent.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.handlers.clear()
    for h in (file_handler, console):
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(getattr(logging, level, logging.INFO))
    # httpx registra cada requisição com o token na URL: só avisos.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
