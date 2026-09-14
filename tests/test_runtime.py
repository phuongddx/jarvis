"""Tests for the frozen PyInstaller runtime seam."""

from __future__ import annotations

import os
from pathlib import Path

from jarvis import runtime


def test_development_mode_has_no_bundled_bin_dir(monkeypatch):
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    assert runtime.is_frozen() is False
    assert runtime.bundled_bin_dir(Path("/does/not/exist")) is None


def test_frozen_mode_resolves_internal_native_bin(monkeypatch, tmp_path):
    native = tmp_path / "_internal" / "native-bin"
    native.mkdir(parents=True)
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "_MEIPASS", tmp_path / "_internal", raising=False)
    assert runtime.is_frozen() is True
    assert runtime.bundled_bin_dir() == native


def test_missing_frozen_directory_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "_MEIPASS", tmp_path, raising=False)
    assert runtime.bundled_bin_dir() is None


def test_initialize_prepends_bundled_bin_exactly_once(monkeypatch, tmp_path):
    native = tmp_path / "native-bin"
    native.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PATH", str(other))
    assert runtime.initialize(base=native) == native
    assert os.environ["PATH"] == os.pathsep.join([str(native), str(other)])
    runtime.initialize(base=native)
    assert os.environ["PATH"] == os.pathsep.join([str(native), str(other)])


def test_initialize_does_not_override_jarvis_zoekt_bin(monkeypatch, tmp_path):
    native = tmp_path / "native-bin"
    native.mkdir()
    override = tmp_path / "custom-zoekt"
    monkeypatch.setenv("JARVIS_ZOEKT_BIN", str(override))
    assert runtime.initialize(base=native) == native
    assert os.environ["JARVIS_ZOEKT_BIN"] == str(override)


def test_initialize_returns_none_in_development(monkeypatch):
    monkeypatch.delenv("JARVIS_ZOEKT_BIN", raising=False)
    assert runtime.initialize(base=None) is None
