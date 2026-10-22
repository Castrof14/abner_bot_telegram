"""Integração REAL com a CLI do Claude Code (usa sua conta; custo pequeno, modelo haiku).

Opt-in:  TELEAGENT_REAL_CLAUDE=1 pytest tests/test_integration_claude.py -s
"""
import asyncio
import os

import pytest

from teleagent.claude_runner import ClaudeRunner
from teleagent.config import DEFAULT_ALLOWED_TOOLS, DEFAULT_DENIED_TOOLS, find_claude_bin
from teleagent.security import review_denial

pytestmark = pytest.mark.skipif(os.environ.get("TELEAGENT_REAL_CLAUDE") != "1",
                                reason="defina TELEAGENT_REAL_CLAUDE=1 para rodar contra a CLI real")


def test_real_cli_write_allowed_rm_denied_then_approved(tmp_path):
    claude = find_claude_bin(os.environ.get("CLAUDE_BIN"))
    assert claude, "CLI do Claude Code não encontrada"
    (tmp_path / "lixo.txt").write_text("apagar\n")
    runner = ClaudeRunner(claude, list(DEFAULT_ALLOWED_TOOLS), list(DEFAULT_DENIED_TOOLS), model="haiku",
                          max_budget_usd=0.5, timeout=300, log_dir=tmp_path / "logs")
    prompt = ("Faça exatamente, uma ferramenta por passo, sem alternativas: 1) crie o arquivo ola.txt com o "
              "conteúdo 'oi' usando a ferramenta Write; 2) rode o comando Bash: rm lixo.txt . Depois responda OK.")
    res = asyncio.run(runner.run(tmp_path, prompt))
    print("\n1ª execução:", res.status, res.text[:200], res.denials)
    assert res.status == "done" and res.session_id
    assert (tmp_path / "ola.txt").exists()
    assert (tmp_path / "lixo.txt").exists(), "rm deveria ter sido negado"
    rm = [d for d in res.denials if "rm" in (d.get("tool_input") or {}).get("command", "")]
    assert rm, "a negação do rm deveria aparecer em permission_denials"

    review = review_denial(rm[0], tmp_path)
    assert review.offer
    res2 = asyncio.run(runner.run(tmp_path, "O proprietário aprovou. Execute exatamente: "
                                  f"{review.summary} . Responda OK.", resume=res.session_id,
                                  extra_allowed=[review.rule]))
    print("2ª execução:", res2.status, res2.text[:200], res2.denials)
    assert res2.status == "done"
    assert not (tmp_path / "lixo.txt").exists(), "após aprovação o comando exato deveria rodar"
