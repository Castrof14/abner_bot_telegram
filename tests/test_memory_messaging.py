import json

from teleagent.memory import MemoryStore, extract_memory_block, merge_states, render_context, render_markdown
from teleagent.messaging import SAFE_LIMIT, split_message


def test_split_message_respects_limit():
    text = "\n".join(f"linha {i} " + "x" * 80 for i in range(400))
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert parts[0].startswith("[1/")
    joined = "\n".join(p.split("\n", 1)[1] for p in parts)
    assert joined.count("linha") == 400


def test_split_message_no_separators():
    parts = split_message("a" * (SAFE_LIMIT * 2 + 10))
    assert all(len(p) <= 4096 for p in parts) and len(parts) == 3


def test_split_short_and_empty():
    assert split_message("oi") == ["oi"]
    assert split_message("") == ["(resposta vazia)"]


def test_extract_memory_block():
    text = 'Feito.\n<teleagent-memory>\n```json\n{"summary": "s", "pending": ["a"]}\n```\n</teleagent-memory>'
    clean, block = extract_memory_block(text)
    assert clean == "Feito." and block == {"summary": "s", "pending": ["a"]}
    clean, block = extract_memory_block("<teleagent-memory>{inválido</teleagent-memory>")
    assert block is None and clean == ""
    assert extract_memory_block("sem bloco") == ("sem bloco", None)


def test_finish_task_updates_items(tmp_path):
    m = MemoryStore(tmp_path, "pc1", ["SEGREDO123"])
    st = m.load("app")
    t = m.start_task(st, "faça X com SEGREDO123")
    assert "SEGREDO123" not in t["prompt"]
    m.finish_task(st, t["id"], "done", "", {"summary": "ok", "decisions": ["d1"], "done": ["x"],
                                             "pending": ["p1", "p2"], "next_steps": ["n1"]})
    assert [i["text"] for i in m.open_items(st, "pending")] == ["p1", "p2"]
    t2 = m.start_task(st, "segunda")
    m.finish_task(st, t2["id"], "done", "", {"done": ["p1"], "pending": ["p2", "p3"], "next_steps": ["n2"]})
    assert [i["text"] for i in m.open_items(st, "pending")] == ["p2", "p3"]
    assert [i["text"] for i in m.open_items(st, "next")] == ["n2"]
    m.save("app", st)
    md = m.markdown_path("app").read_text()
    assert "p3" in md and "Histórico" in md
    assert json.loads(m.state_path("app").read_text())["project"] == "app"


def test_interrupted_task_creates_pending_and_is_resolved(tmp_path):
    m = MemoryStore(tmp_path, "pc1")
    st = m.load("app")
    t = m.start_task(st, "migrar banco")
    m.finish_task(st, t["id"], "interrupted", "", None)
    assert any("migrar banco" in i["text"] for i in m.open_items(st, "pending"))
    t2 = m.start_task(st, "migrar banco")
    m.finish_task(st, t2["id"], "done", "feito", None)
    assert not any("migrar banco" in i["text"] for i in m.open_items(st, "pending"))


def test_merge_states_union_and_newest_wins(tmp_path):
    a = MemoryStore(tmp_path / "a", "pc1")
    b = MemoryStore(tmp_path / "b", "pc2")
    base = a.load("app")
    item = a.add_item(base, "pending", "comum")
    sa = json.loads(json.dumps(base))
    sb = json.loads(json.dumps(base))
    a.add_item(sa, "decision", "só no pc1")
    b.add_item(sb, "decision", "só no pc2")
    b.close_item(sb, sb["items"][item["id"]], "resolved")
    sb["items"][item["id"]]["updated"] = "9999-01-01T00:00:00+00:00"
    merged = merge_states(sa, sb)
    texts = {i["text"] for i in merged["items"].values()}
    assert {"comum", "só no pc1", "só no pc2"} <= texts
    assert merged["items"][item["id"]]["status"] == "resolved"
    assert "só no pc2" in render_markdown(merged)
    assert "só no pc1" in render_context(merged)
