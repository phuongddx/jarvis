"""Tests for the PyInstaller argv[0] launcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "launcher.py"
spec = importlib.util.spec_from_file_location("jarvis_packaging_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_selects_cli_for_jarvis_names():
    assert launcher.select_entrypoint("/opt/homebrew/bin/jarvis") == "cli"
    assert launcher.select_entrypoint("jarvis") == "cli"


def test_selects_server_for_jarvis_server_name():
    assert launcher.select_entrypoint("/opt/homebrew/bin/jarvis-server") == "server"


def test_unknown_name_fails_loudly(capsys):
    try:
        launcher.select_entrypoint("/opt/homebrew/bin/unknown")
    except SystemExit as exc:
        assert exc.code == 2
        assert "jarvis-server" in capsys.readouterr().err
    else:
        raise AssertionError("unknown argv[0] must exit")


def test_run_dispatches_server(monkeypatch, tmp_path):
    fake = tmp_path / "native-bin"
    fake.mkdir()
    monkeypatch.setattr(launcher.sys, "argv", ["/somewhere/jarvis-server"])
    calls: list[str] = []
    result = launcher.run(
        cli_main=lambda: calls.append("cli") or 0,
        server_main=lambda: calls.append("server") or None,
        runtime_initialize=lambda base=None: fake,
    )
    assert result is None
    assert calls == ["server"]
