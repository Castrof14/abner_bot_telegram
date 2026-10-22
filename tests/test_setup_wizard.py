import os

from teleagent import config, setup_wizard


def test_wizard_writes_valid_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    (tmp_path / ".env.example").write_text((config.BASE_DIR / ".env.example").read_text())
    projects = tmp_path / "projetos"
    monkeypatch.setattr(setup_wizard, "ENV_FILE", env)
    token = "123456789:AAH4kE9xQwErTyUiOpAsDfGhJkLzXcVbNmQ"
    answers = iter([
        "n",                 # não detectar automaticamente
        "424242",            # ID do dono
        str(projects),       # diretório de projetos
        "s",                 # criar diretório
        "",                  # sem repositório de sync
        "notebook-casa",     # nome da máquina
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(setup_wizard.getpass, "getpass", lambda prompt="": token)
    monkeypatch.setattr(setup_wizard, "_tg", lambda t, m, **k: {"username": "meu_bot", "first_name": "Bot"})
    assert setup_wizard.run_wizard() == 0
    assert projects.is_dir()
    if os.name != "nt":
        assert oct(env.stat().st_mode & 0o777) == "0o600"
    cfg = config.load_config(env, tmp_path)
    assert cfg.telegram_token == token and cfg.owner_id == 424242
    assert cfg.projects_dirs == [projects.resolve()] and cfg.machine_name == "notebook-casa"


def test_config_rejects_home_and_bad_owner(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_OWNER_ID=abc\nPROJECTS_DIRS={os.path.expanduser('~')}\n")
    try:
        config.load_config(env, tmp_path)
        raise AssertionError("deveria falhar")
    except config.ConfigError as e:
        assert "OWNER" in str(e) and "amplo demais" in str(e)
