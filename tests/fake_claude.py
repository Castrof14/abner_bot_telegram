"""Simula a CLI do Claude Code (`-p --output-format stream-json`) para testes.

Formato dos eventos copiado de uma execução real da CLI 2.1.x.
Comportamento controlado por FAKE_CLAUDE_MODE: ok | deny | sleep | crash | badresume
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_ARGS")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "prompt": prompt, "cwd": os.getcwd()}) + "\n")

if "--version" in args:
    print("9.9.9 (Claude Code)")
    sys.exit(0)

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())


def emit(obj):
    print(json.dumps(obj), flush=True)


if mode == "badresume" and "--resume" in args:
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
          "session_id": sid, "result": "", "permission_denials": [], "errors": ["No conversation found"]})
    sys.exit(1)

if mode == "crash":
    print("fatal: algo quebrou", file=sys.stderr)
    sys.exit(2)

emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": sid, "model": "fake"})
emit({"type": "assistant", "session_id": sid, "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}})
if mode == "sleep":
    time.sleep(60)

denials = []
if mode == "deny" and "--resume" not in args:
    emit({"type": "assistant", "session_id": sid, "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "rm build.log"}}]}})
    emit({"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "t2",
          "session_id": sid})
    denials = [{"tool_name": "Bash", "tool_use_id": "t2", "tool_input": {"command": "rm build.log"}},
               {"tool_name": "Bash", "tool_use_id": "t3", "tool_input": {"command": "sudo rm -rf /"}},
               {"tool_name": "Write", "tool_use_id": "t4", "tool_input": {"file_path": "/etc/hosts"}}]

block = {"summary": "Criei o README", "decisions": ["Usar pytest"], "done": ["README criado"],
         "pending": ["Escrever testes"], "next_steps": ["Configurar CI"]}
text = "Tudo certo, criei o README.\n<teleagent-memory>\n" + json.dumps(block, ensure_ascii=False) + "\n</teleagent-memory>"
emit({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": text}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "session_id": sid,
      "total_cost_usd": 0.0123, "result": text, "permission_denials": denials})
