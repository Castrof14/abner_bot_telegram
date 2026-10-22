import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from teleagent.config import DEFAULT_ALLOWED_TOOLS, DEFAULT_DENIED_TOOLS, Config  # noqa: E402

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def fake_claude(tmp_path) -> str:
    """Executável que simula o claude (wrapper com o Python atual)."""
    if os.name == "nt":
        pytest.skip("wrapper de teste é POSIX")
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{FAKE}' \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


@pytest.fixture
def cfg(tmp_path, fake_claude) -> Config:
    projects = tmp_path / "projetos"
    (projects / "app").mkdir(parents=True)
    base = tmp_path / "teleagent"
    base.mkdir()
    return Config(telegram_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", owner_id=111,
                  projects_dirs=[projects], machine_name="pc-teste", claude_bin=fake_claude,
                  allowed_tools=list(DEFAULT_ALLOWED_TOOLS), denied_tools=list(DEFAULT_DENIED_TOOLS),
                  base_dir=base, task_timeout=30)
