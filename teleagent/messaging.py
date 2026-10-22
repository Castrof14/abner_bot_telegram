"""Divisão de mensagens longas para o limite do Telegram (4096 caracteres)."""
from __future__ import annotations

TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900
# Acima disso a resposta vai como arquivo, para não inundar o chat.
DOCUMENT_THRESHOLD = 12000


def split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Quebra preferencialmente em parágrafos, depois linhas, depois espaços."""
    text = text or ""
    if not text.strip():
        return ["(resposta vazia)"]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx >= limit // 2:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        chunks.append(rest)
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"[{i}/{total}]\n{c}" for i, c in enumerate(chunks, 1)]
    return chunks


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
