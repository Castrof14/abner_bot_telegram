import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

from teleagent.gitsync import GitSync
from teleagent.instance_lock import InstanceLockError, Lease, LocalInstanceLock
from teleagent.memory import MemoryStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    return str(bare)


def machines(tmp_path, remote):
    s1 = GitSync(tmp_path / "pc1" / "sync", remote, "main", "pc1")
    s2 = GitSync(tmp_path / "pc2" / "sync", remote, "main", "pc2")
    return s1, s2


def test_sync_between_two_machines_with_semantic_conflict(tmp_path, remote):
    s1, s2 = machines(tmp_path, remote)
    assert run(s1.ensure_repo()).ok
    m1 = MemoryStore(s1.dir, "pc1")
    st = m1.load("app")
    m1.add_item(st, "objective", "lançar v1")
    m1.save("app", st)
    r = run(s1.sync())
    assert r.ok and r.pushed, r.message

    assert run(s2.ensure_repo()).ok
    m2 = MemoryStore(s2.dir, "pc2")
    assert "lançar v1" in m2.markdown_path("app").read_text()

    # Alterações concorrentes no MESMO state.json
    st1 = m1.load("app"); m1.add_item(st1, "decision", "decisão do pc1"); m1.save("app", st1)
    st2 = m2.load("app"); m2.add_item(st2, "decision", "decisão do pc2"); m2.save("app", st2)
    assert run(s1.sync()).pushed
    r2 = run(s2.sync())
    assert r2.ok and r2.pushed, r2.message
    merged = m2.load("app")
    texts = {i["text"] for i in merged["items"].values()}
    assert {"lançar v1", "decisão do pc1", "decisão do pc2"} <= texts
    md = m2.markdown_path("app").read_text()
    assert "decisão do pc1" in md and "<<<<<<<" not in md

    r1 = run(s1.sync())
    assert r1.ok and r1.pulled and "app" in r1.changed_projects
    assert "decisão do pc2" in m1.markdown_path("app").read_text()


def test_unresolvable_conflict_keeps_local_and_does_not_push(tmp_path, remote):
    s1, s2 = machines(tmp_path, remote)
    run(s1.ensure_repo()); run(s1.sync())
    run(s2.ensure_repo())
    (s1.dir / "memory" / "notas.txt").write_text("versão pc1\n")
    (s2.dir / "memory" / "notas.txt").write_text("versão pc2\n")
    assert run(s1.sync()).pushed
    r = run(s2.sync())
    assert not r.ok and "memory/notas.txt" in r.conflicts
    assert (s2.dir / "memory" / "notas.txt").read_text() == "versão pc2\n"   # nada sobrescrito
    assert not (s2.dir / ".git" / "MERGE_HEAD").exists()                      # mescla abortada
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=s2.dir, capture_output=True, text=True).stdout
    assert "pc2" in log                                                        # commit local preservado
    remote_log = subprocess.run(["git", "--git-dir", remote, "log", "--format=%s", "main"],
                                capture_output=True, text=True).stdout
    assert "[pc2]" not in remote_log.splitlines()[0]


def test_commit_blocked_when_secret_present(tmp_path):
    s = GitSync(tmp_path / "sync", "", "main", "pc1")
    assert run(s.ensure_repo()).ok
    (s.dir / "memory" / "vazou.md").write_text("token=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    r = run(s.sync())
    assert not r.ok and "segredo" in r.message
    st = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=s.dir, capture_output=True, text=True).stdout
    assert "vazou.md" in st  # continua no disco, só não foi commitado


def test_sync_gitignore_excludes_other_files(tmp_path):
    s = GitSync(tmp_path / "sync", "", "main", "pc1")
    run(s.ensure_repo())
    (s.dir / "sessao-claude.jsonl").write_text("{}")
    (s.dir / "memory" / "x").mkdir()
    (s.dir / "memory" / "x" / "state.json").write_text("{}")
    run(s.sync())
    files = subprocess.run(["git", "ls-files"], cwd=s.dir, capture_output=True, text=True).stdout
    assert "memory/x/state.json" in files and "sessao-claude.jsonl" not in files


def test_local_lock_prevents_second_instance(tmp_path):
    lock = LocalInstanceLock(tmp_path / "t.lock")
    lock.acquire()
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parent.parent)!r})
        from teleagent.instance_lock import LocalInstanceLock, InstanceLockError
        try:
            LocalInstanceLock(__import__('pathlib').Path({str(tmp_path / 't.lock')!r})).acquire()
            print('ACQUIRED')
        except InstanceLockError:
            print('BLOCKED')
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "BLOCKED"
    lock.release()
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "ACQUIRED"


def test_lease_rules(tmp_path):
    a = Lease(tmp_path, "pc1", stale_hours=3)
    b = Lease(tmp_path, "pc2", stale_hours=3)
    assert a.check().can_start
    a.write()
    chk = b.check()
    assert not chk.can_start and "pc1" in chk.message
    assert b.check(takeover=True).can_start
    assert b.owned_by_other()["machine"] == "pc1"
    data = json.loads(a.path.read_text()); data["heartbeat"] = "2000-01-01T00:00:00+00:00"
    a.path.write_text(json.dumps(data))
    assert b.check().can_start  # lease inativa
    a.write(released=True)
    assert b.check().can_start and b.owned_by_other() is None
