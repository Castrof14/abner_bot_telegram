import asyncio
import json
import os

from teleagent import deps
from teleagent.claude_runner import ClaudeRunner


def test_detect_managers(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "go.mod").write_text("")
    steps = deps.detect(tmp_path)
    managers = [s.manager for s in steps]
    assert managers[:1] == ["pnpm"]
    assert "venv" in managers and "pip" in managers and "go" in managers
    assert deps.looks_uninstalled(tmp_path)


def test_detect_lockfile_priority(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "yarn.lock").write_text("")
    assert [s.argv for s in deps.detect(tmp_path)] == [["yarn", "install"]]
    (tmp_path / "uv.lock").write_text("")
    assert ["uv", "sync"] in [s.argv for s in deps.detect(tmp_path)]


def test_run_steps_executes_without_shell(tmp_path):
    step = deps.InstallStep("python", [os.sys.executable, "-c", "print('instalado')"], "teste")
    res = asyncio.run(deps.run_steps(tmp_path, [step], tmp_path / "log.txt"))
    assert res[0][1] == 0 and "instalado" in res[0][2]
    missing = deps.InstallStep("x", ["comando-que-nao-existe-123"], "t", available=False)
    res = asyncio.run(deps.run_steps(tmp_path, [missing], tmp_path / "log.txt"))
    assert res[0][1] == 127


def _runner(cfg, **kw):
    return ClaudeRunner(cfg.claude_bin, cfg.allowed_tools, cfg.denied_tools, timeout=kw.get("timeout", 30),
                        log_dir=cfg.log_dir / "tasks", max_budget_usd=kw.get("budget"))


def test_build_args_uses_documented_flags(cfg):
    r = _runner(cfg, budget=2)
    args = r.build_args(resume="abc", extra_allowed=["Bash(rm build.log)"])
    assert args[1:4] == ["-p", "--output-format", "stream-json"]
    assert "--dangerously-skip-permissions" not in args and "bypassPermissions" not in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert "Bash(rm build.log)" in args and "Bash(rm:*)" not in args and "Bash(git push:*)" in args
    assert args[args.index("--resume") + 1] == "abc"
    assert args[args.index("--max-budget-usd") + 1] == "2"
    assert "--add-dir" not in args
    args = r.build_args(extra_dirs=["/tmp/a", "/tmp/b"])
    i = args.index("--add-dir")
    assert args[i + 1:i + 3] == ["/tmp/a", "/tmp/b"]


def test_run_parses_stream_and_denials(cfg, monkeypatch, tmp_path):
    argfile = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(argfile))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "deny")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "nao-deve-vazar")
    seen = []

    async def prog(p):
        seen.append(p.last_action)

    project = cfg.projects_dirs[0] / "app"
    res = asyncio.run(_runner(cfg).run(project, "PROMPT VIA STDIN", on_progress=prog))
    assert res.status == "done" and res.session_id and res.cost_usd == 0.0123
    assert [d["tool_input"]["command"] for d in res.denials if d["tool_name"] == "Bash"] == ["rm build.log", "sudo rm -rf /"]
    assert any("Bash: ls" in s for s in seen)
    assert res.log_path.exists() and res.tool_calls == 2
    call = json.loads(argfile.read_text().splitlines()[0])
    assert call["prompt"] == "PROMPT VIA STDIN" and call["cwd"] == str(project)


def test_run_does_not_leak_bot_token(cfg, monkeypatch, tmp_path):
    script = tmp_path / "envdump"
    script.write_text(f"#!/bin/sh\nenv > '{tmp_path / 'env.txt'}'\n")
    script.chmod(0o755)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "nao-deve-vazar")
    r = ClaudeRunner(str(script), [], [], timeout=10)
    asyncio.run(r.run(tmp_path, "x"))
    assert "nao-deve-vazar" not in (tmp_path / "env.txt").read_text()


def test_cancel_and_timeout(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    project = cfg.projects_dirs[0] / "app"

    async def cancel_soon():
        r = _runner(cfg)
        task = asyncio.create_task(r.run(project, "x"))
        await asyncio.sleep(1.5)
        assert r.running
        assert await r.cancel()
        return await task

    res = asyncio.run(cancel_soon())
    assert res.status == "cancelled"
    res = asyncio.run(_runner(cfg, timeout=2).run(project, "x"))
    assert res.status == "timeout"


def test_crash_reports_stderr(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    res = asyncio.run(_runner(cfg).run(cfg.projects_dirs[0] / "app", "x"))
    assert res.status == "error" and "algo quebrou" in res.text
