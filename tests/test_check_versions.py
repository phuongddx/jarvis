"""Tests for the single-source release version guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_versions.py"
spec = importlib.util.spec_from_file_location("check_versions", SCRIPT)
check_versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_versions)


def test_pyproject_is_the_single_declared_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "jarvis-mcp"\nversion = "0.11.0"\n'
    )
    assert check_versions.read_declared_versions(tmp_path) == {
        "pyproject.toml [project].version": "0.11.0"
    }
    assert check_versions.check(tmp_path) == []


def test_missing_or_invalid_pyproject_is_reported(tmp_path):
    problems = check_versions.check(tmp_path)
    assert len(problems) == 1
    assert "could not read release version" in problems[0]
