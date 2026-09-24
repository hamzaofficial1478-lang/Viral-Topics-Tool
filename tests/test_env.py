"""Dependency-free .env loader for API keys."""

import os

from shortforge.utils import load_env_file


def test_loads_keys_without_overriding_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "ANTHROPIC_API_KEY=sk-from-file\n"
        'QUOTED="with spaces"\n'
        "export EXPORTED=yes\n"
        "ALREADY_SET=from-file\n"
    )
    monkeypatch.setenv("ALREADY_SET", "from-shell")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.delenv("EXPORTED", raising=False)

    n = load_env_file(str(env))

    assert n == 3  # ALREADY_SET is skipped (shell wins)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-from-file"
    assert os.environ["QUOTED"] == "with spaces"      # quotes stripped
    assert os.environ["EXPORTED"] == "yes"            # export prefix handled
    assert os.environ["ALREADY_SET"] == "from-shell"  # not overwritten


def test_missing_file_is_a_noop(tmp_path):
    assert load_env_file(str(tmp_path / "does-not-exist.env")) == 0
