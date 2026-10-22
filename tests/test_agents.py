"""Agentes de IA criados e configurados pelo Telegram."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from teleagent.agents import Agent, AgentStore, is_free_model
from tests.test_bot import _anoop, ctx, make_agent, update


def press(agent, data):
    q = SimpleNamespace(data=data, answer=_anoop, edit_message_reply_markup=_anoop, edit_message_text=_anoop)
    asyncio.run(agent.on_callback(update(callback=q), ctx()))


def last_markup_data(agent):
    markup = next(m["reply_markup"] for m in reversed(agent.tg.sent) if m.get("reply_markup"))
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_store_persists_and_validates(tmp_path):
    store = AgentStore(tmp_path / "agents.json")
    store.put(Agent(name="revisor", runner="opencode", model="opencode/big-pickle", instructions="x" * 5000))
    again = AgentStore(tmp_path / "agents.json")
    a = again.get("revisor")
    assert a.runner == "opencode" and a.model == "opencode/big-pickle" and len(a.instructions) == 4000
    for bad in (Agent(name="a/b"), Agent(name="ok", runner="gpt")):
        with pytest.raises(ValueError):
            store.put(bad)
    assert store.delete("revisor") and not store.delete("revisor")
    assert is_free_model("opencode/glm-5.3-flash-free") and is_free_model("opencode/big-pickle")
    assert not is_free_model("anthropic/claude-sonnet-4-5")


def test_create_claude_agent_via_telegram_and_run(cfg, monkeypatch, tmp_path):
    argfile = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(argfile))
    agent = make_agent(cfg)
    agent.state.selected_project = "app"

    asyncio.run(agent.cmd_agente(update(), ctx("novo", "revisor")))
    runner_btn = next(d for d in last_markup_data(agent) if d.endswith(":claude"))
    press(agent, runner_btn)
    opus = next(d for d in last_markup_data(agent)
                if agent.pending[d.split(":")[1]]["models"][int(d.split(":")[2])] == "opus")
    press(agent, opus)
    assert agent._awaiting["kind"] == "agent_instructions"
    asyncio.run(agent.on_text(update(text="Você é um revisor rigoroso."), ctx()))
    assert agent.task is None  # o texto virou instruções, não tarefa
    assert agent.agents.get("revisor").instructions == "Você é um revisor rigoroso."

    press(agent, "ag:use:revisor")
    assert agent.state.agent_for("app") == "revisor"

    async def run():
        await agent.on_text(update(text="revise o código"), ctx())
        await agent.task
    asyncio.run(run())
    call = json.loads(argfile.read_text().splitlines()[-1])
    assert call["argv"][call["argv"].index("--model") + 1] == "opus"
    assert "revisor rigoroso" in call["prompt"] and "revise o código" in call["prompt"]


def test_create_opencode_agent_with_typed_model(cfg, monkeypatch):
    agent = make_agent(cfg)
    agent.opencode.opencode_bin = "/fake/opencode"

    async def fake_models(bin_):
        return ["opencode/big-pickle", "opencode/glm-5.3-flash-free"]
    monkeypatch.setattr("teleagent.bot.opencode_models", fake_models)

    asyncio.run(agent.cmd_agente(update(), ctx("novo", "gratis")))
    press(agent, next(d for d in last_markup_data(agent) if d.endswith(":opencode")))
    assert "🆓" in str(agent.tg.sent[-1]["reply_markup"])
    press(agent, next(d for d in last_markup_data(agent) if d.endswith(":x")))
    asyncio.run(agent.on_text(update(text="opencode/glm-5.3-flash-free"), ctx()))
    press(agent, "agi:skip:gratis")
    a = agent.agents.get("gratis")
    assert a.runner == "opencode" and a.model == "opencode/glm-5.3-flash-free" and a.instructions == ""
    assert agent._awaiting is None

    # Sem projeto selecionado, "usar" define o agente padrão.
    press(agent, "ag:use:gratis")
    assert agent._active_profile("app") == ("opencode", "opencode/glm-5.3-flash-free", a)


def test_session_not_shared_between_runners(cfg):
    agent = make_agent(cfg)
    agent.state.set_session("app", "sess-claude", "claude")
    assert agent.state.session_for("app", "claude") == "sess-claude"
    assert agent.state.session_for("app", "opencode") is None


def test_delete_agent_clears_selection(cfg):
    agent = make_agent(cfg)
    agent.state.selected_project = "app"
    agent.agents.put(Agent(name="tmp"))
    asyncio.run(agent.cmd_agente(update(), ctx("usar", "tmp")))
    assert agent.state.agent_for("app") == "tmp"
    asyncio.run(agent.cmd_agente(update(), ctx("apagar", "tmp")))
    tok = next(k for k, v in agent.pending.items() if v["action"] == "agent_delete")
    press(agent, f"ok:{tok}")
    assert agent.agents.get("tmp") is None and agent.state.agent_for("app") is None


def test_opencode_bad_session_falls_back_to_new(cfg, tmp_path):
    import stat
    import sys
    fake = tmp_path / "bin" / "opencode"
    fake.write_text(f"""#!{sys.executable}
import json, sys
if "--session" in sys.argv:
    print(json.dumps({{"type": "error", "sessionID": "ses_x", "error": {{"name": "UnknownError",
          "data": {{"message": "Unexpected server error."}}}}}}))
else:
    print(json.dumps({{"type": "text", "sessionID": "ses_new", "part": {{"type": "text", "text": "feito"}}}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    agent = make_agent(cfg)
    agent.opencode.opencode_bin = str(fake)
    agent.state.selected_project = "app"
    agent.agents.put(Agent(name="isa", runner="opencode", model="opencode/big-pickle"))
    agent.state.set_agent("app", "isa")
    agent.state.set_session("app", "ses_velha", "opencode")

    async def run():
        await agent.on_text(update(text="faça algo"), ctx())
        await agent.task
    asyncio.run(run())
    assert any("feito" in m["text"] for m in agent.tg.sent)
    assert agent.state.session_for("app", "opencode") == "ses_new"
