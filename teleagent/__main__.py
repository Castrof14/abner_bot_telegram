"""Ponto de entrada: python -m teleagent [run|setup|check] [--takeover]"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teleagent", description="TeleAgent — Claude Code pelo Telegram")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="inicia o bot (padrão)")
    run.add_argument("--takeover", action="store_true",
                     help="assume a instância mesmo que outra máquina conste como ativa (use se ela estiver desligada)")
    sub.add_parser("setup", help="assistente de configuração inicial")
    sub.add_parser("check", help="diagnóstico da configuração e das dependências")
    args = parser.parse_args(argv)

    if args.cmd == "setup":
        from .setup_wizard import run_wizard
        return run_wizard()
    if args.cmd == "check":
        from .diagnostics import run_check
        return asyncio.run(run_check())

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Configuração inválida: {e}\nRode o assistente: python -m teleagent setup", file=sys.stderr)
        return 2

    from telegram.error import InvalidToken, NetworkError

    from .bot import StartupError, TeleAgentBot
    from .logging_setup import setup_logging
    setup_logging(cfg.log_dir, cfg.log_level, [cfg.telegram_token])
    if not cfg.claude_bin:
        print("Claude Code CLI não encontrado. Instale-o ou defina CLAUDE_BIN no .env.", file=sys.stderr)
        return 2
    bot = TeleAgentBot(cfg, takeover=getattr(args, "takeover", False))
    app = bot.build()
    try:
        app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
    except StartupError as e:
        print(f"\nTeleAgent não iniciado: {e}", file=sys.stderr)
        return 3
    except InvalidToken:
        print("\nO Telegram recusou TELEGRAM_BOT_TOKEN. Confira o token no @BotFather e rode o assistente "
              "(python -m teleagent setup).", file=sys.stderr)
        return 3
    except NetworkError as e:
        print(f"\nSem conexão com o Telegram: {type(e).__name__}. Verifique a internet e tente novamente.",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
