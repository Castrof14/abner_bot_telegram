"""Testes do fluxo do bot com um Bot do Telegram falso e a CLI do Claude simulada."""
import asyncio
import json
from types import SimpleNamespace

from telegram.ext import CommandHandler

from teleagent.bot import COMMANDS, TeleAgentBot


class FakeBot:
    def __init__(self):
        self.sent, self.edits, self.docs = [], [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id, message_id):
        self.edits.append(text)

    async def send_document(self, chat_id, document, filename):
        self.docs.append((filename, document.read()))


class Msg:
    def __init__(self, text=""):
        self.text, self.replies = text, []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(message_id=99)

    reply_html = reply_text


def update(user_id=111, chat_type="private", text="", callback=None):
    msg = Msg(text)
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username="u"),
                           effective_chat=SimpleNamespace(id=user_id, type=chat_type),
                           effective_message=msg, callback_query=callback)


def ctx(*args):
    return SimpleNamespace(args=list(args))


def make_agent(cfg):
    agent = TeleAgentBot(cfg)
    agent.tg = FakeBot()
    asyncio.run(agent.sync.ensure_repo())
    return agent


def test_all_commands_registered(cfg):
    app = TeleAgentBot(cfg).build()
    names = {c for h in app.handlers[0] if isinstance(h, CommandHandler) for c in h.commands}
    assert {c for c, _ in COMMANDS} <= names


def test_rejects_non_owner_and_groups(cfg):
    agent = make_agent(cfg)
    u = update(user_id=999)
    asyncio.run(agent.cmd_status(u, ctx()))
    assert u.effective_message.replies == ["⛔ Acesso negado."]
    u = update(user_id=999, text="apague tudo")
    asyncio.run(agent.on_text(u, ctx()))
    assert agent.task is None
    g = update(user_id=111, chat_type="group")
    asyncio.run(agent.cmd_help(g, ctx()))
    assert g.effective_message.replies == []  # grupos: ignorado mesmo para o dono


def test_select_project_rejects_traversal(cfg):
    agent = make_agent(cfg)
    asyncio.run(agent.cmd_projetos(update(), ctx("../../etc")))
    assert agent.state.selected_project is None
    assert "inválido" in agent.tg.sent[-1]["text"]
    asyncio.run(agent.cmd_projetos(update(), ctx("app")))
    assert agent.state.selected_project == "app"


def test_task_flow_memory_and_approval(cfg, monkeypatch, tmp_path):
    argfile = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS", str(argfile))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "deny")
    agent = make_agent(cfg)
    agent.state.selected_project = "app"

    async def scenario():
        u = update(text="crie um README")
        await agent.on_text(u, ctx())
        await agent.task
        # Segunda mensagem enquanto ocupado é recusada
        return u

    asyncio.run(scenario())
    texts = [m["text"] for m in agent.tg.sent]
    final = next(t for t in texts if t.startswith("✅ Concluído"))
    assert "Tudo certo" in final and "teleagent-memory" not in final
    approval = next(m for m in agent.tg.sent if m["text"].startswith("🔐"))
    assert "rm build.log" in approval["text"] and approval.get("reply_markup") is not None
    assert "sudo rm -rf /" in approval["text"] and "Não podem ser aprovadas" in approval["text"]
    assert "/etc/hosts" in approval["text"]

    st = agent.memory.load("app")
    assert [i["text"] for i in agent.memory.open_items(st, "pending")] == ["Escrever testes"]
    assert [i["text"] for i in agent.memory.open_items(st, "next")] == ["Configurar CI"]
    assert agent.state.running_task is None
    sid = agent.state.session_for("app")
    assert sid

    first = json.loads(argfile.read_text().splitlines()[0])
    assert "crie um README" in first["prompt"] and "<teleagent-memory>" in first["prompt"]

    # Aprova pelo botão
    tok, item = next((k, v) for k, v in agent.pending.items() if v["action"] == "approve")
    assert item["rules"] == ["Bash(rm build.log)"]

    async def approve():
        q = SimpleNamespace(data=f"ok:{tok}", answer=_anoop, edit_message_reply_markup=_anoop,
                            edit_message_text=_anoop)
        await agent.on_callback(update(callback=q), ctx())
        await agent.task

    asyncio.run(approve())
    last = json.loads(argfile.read_text().splitlines()[-1])
    argv = last["argv"]
    assert argv[argv.index("--resume") + 1] == sid
    assert "Bash(rm build.log)" in argv and "Bash(rm:*)" not in argv and "Bash(git push:*)" in argv
    assert "APROVOU" in last["prompt"]
    assert tok not in agent.pending  # token de uso único


async def _anoop(*a, **k):
    return None


def test_refusal_is_recorded(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "deny")
    agent = make_agent(cfg)
    agent.state.selected_project = "app"

    async def go():
        await agent.on_text(update(text="limpe"), ctx())
        await agent.task
        tok = next(k for k, v in agent.pending.items() if v["action"] == "approve")
        q = SimpleNamespace(data=f"no:{tok}", answer=_anoop, edit_message_reply_markup=_anoop)
        await agent.on_callback(update(callback=q), ctx())

    asyncio.run(go())
    st = agent.memory.load("app")
    assert any("recusou" in i["text"] for i in agent.memory.open_items(st, "decision"))


def test_busy_rejects_second_task(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    agent = make_agent(cfg)
    agent.state.selected_project = "app"

    async def go():
        await agent.on_text(update(text="tarefa 1"), ctx())
        await asyncio.sleep(1.0)
        u2 = update(text="tarefa 2")
        await agent.on_text(u2, ctx())
        p = update()
        await agent.cmd_parar(p, ctx())
        await agent.task
        return u2

    u2 = asyncio.run(go())
    assert "Já há uma tarefa" in u2.effective_message.replies[0]
    assert any(m["text"].startswith("🛑 Cancelado") for m in agent.tg.sent)
    st = agent.memory.load("app")
    assert any("tarefa 1" in i["text"] for i in agent.memory.open_items(st, "pending"))


def test_bad_resume_falls_back_to_new_session(cfg, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "badresume")
    agent = make_agent(cfg)
    agent.state.selected_project = "app"
    agent.state.set_session("app", "sessao-de-outro-pc")

    async def go():
        await agent.on_text(update(text="oi"), ctx())
        await agent.task

    asyncio.run(go())
    assert agent.state.session_for("app") not in (None, "sessao-de-outro-pc")
    assert any(m["text"].startswith("✅ Concluído") for m in agent.tg.sent)


def test_recover_interrupted_task(cfg):
    agent = make_agent(cfg)
    st = agent.memory.load("app")
    t = agent.memory.start_task(st, "refatorar módulo X")
    agent.memory.save("app", st)
    agent.state.running_task = {"project": "app", "task_id": t["id"], "prompt": "refatorar módulo X",
                                "started": t["started"]}
    asyncio.run(agent._recover_interrupted())
    assert agent.state.running_task is None
    st = agent.memory.load("app")
    assert st["tasks"][t["id"]]["status"] == "interrupted"
    assert "interrompida" in agent.tg.sent[-1]["text"]


def test_long_response_goes_as_document(cfg):
    agent = make_agent(cfg)
    asyncio.run(agent.send_long("x" * 20000))
    assert agent.tg.docs and len(agent.tg.docs[0][1]) == 20000


def test_notify_redacts_token(cfg):
    agent = make_agent(cfg)
    asyncio.run(agent.notify(f"vazou {cfg.telegram_token}"))
    assert cfg.telegram_token not in agent.tg.sent[-1]["text"]


class StartupBot(FakeBot):
    def __init__(self, conflict=False):
        super().__init__()
        self.conflict = conflict
        self.commands = None

    async def get_updates(self, **kw):
        if self.conflict:
            from telegram.error import Conflict
            raise Conflict("terminated by other getUpdates request")
        return []

    async def set_my_commands(self, cmds):
        self.commands = cmds


def _startup(agent, bot):
    agent.tg = bot
    return agent._post_init(SimpleNamespace(bot=bot))


def test_startup_lease_and_shutdown_release(cfg, tmp_path):
    import dataclasses
    import pytest
    from teleagent.bot import StartupError

    a = TeleAgentBot(cfg)
    asyncio.run(_startup(a, StartupBot()))
    lease = a.lease.read()
    assert lease["machine"] == "pc-teste" and not lease["released"]
    assert a.tg.commands and "online" in a.tg.sent[-1]["text"]

    # Segunda máquina usando a mesma pasta de memória: recusada pela lease
    cfg2 = dataclasses.replace(cfg, machine_name="outro-pc", base_dir=cfg.base_dir)
    b = TeleAgentBot(cfg2)
    b.local_lock = type(b.local_lock)(tmp_path / "outro.lock")
    with pytest.raises(StartupError, match="pc-teste"):
        asyncio.run(_startup(b, StartupBot()))
    a._heartbeat_task = None
    asyncio.run(a.cleanup())
    assert a.lease.read()["released"] is True

    # Depois de liberada, a outra máquina pode iniciar; 409 do Telegram ainda impede duplicidade
    c = TeleAgentBot(cfg2)
    c.local_lock = type(c.local_lock)(tmp_path / "outro2.lock")
    with pytest.raises(StartupError, match="409"):
        asyncio.run(_startup(c, StartupBot(conflict=True)))
    assert c.lease.read()["released"] is True


def test_criar_creates_and_selects_project(cfg, tmp_path):
    import dataclasses
    docs = tmp_path / "Documents"
    docs.mkdir()
    cfg2 = dataclasses.replace(cfg, projects_dirs=cfg.projects_dirs + [docs])
    agent = make_agent(cfg2)
    u = update()
    asyncio.run(agent.cmd_criar(u, ctx("meu-site")))
    tok = next(k for k, v in agent.pending.items() if v["action"] == "create")

    async def choose():
        q = SimpleNamespace(data=f"mk:{tok}:1", answer=_anoop, edit_message_reply_markup=_anoop)
        await agent.on_callback(update(callback=q), ctx())

    asyncio.run(choose())
    assert (docs / "meu-site").is_dir()
    assert agent.state.selected_project == "meu-site"
    for bad in ("../fora", "a/b", ""):
        u = update()
        asyncio.run(agent.cmd_criar(u, ctx(*([bad] if bad else []))))
        assert u.effective_message.replies
    assert not (tmp_path / "fora").exists()


def test_bot_dir_hidden_from_projects(cfg):
    import dataclasses
    cfg2 = dataclasses.replace(cfg, projects_dirs=[cfg.base_dir.parent])
    agent = make_agent(cfg2)
    asyncio.run(agent.cmd_projetos(update(), ctx("teleagent")))
    assert agent.state.selected_project is None
    assert "protegida" in agent.tg.sent[-1]["text"]
