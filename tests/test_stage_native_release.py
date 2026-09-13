"""Tests for shaping and archiving PyInstaller output."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage_native_release.py"
spec = importlib.util.spec_from_file_location("stage_native_release", SCRIPT)
stager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stager)


def make_app(parent: Path) -> Path:
    app_dir = parent / "app"
    app = app_dir / "jarvis"
    (app / "_internal").mkdir(parents=True)
    launcher = app / "jarvis"
    launcher.write_text("#!/bin/sh\ntrue\n")
    launcher.chmod(0o755)
    (app / "_internal" / "base_library.zip").write_bytes(b"runtime")
    return app_dir


def make_native(parent: Path) -> Path:
    native = parent / "native"
    native.mkdir()
    for name in ("scip", "zoekt-git-index", "zoekt-webserver"):
        path = native / name
        path.write_text("#!/bin/sh\ntrue\n")
        path.chmod(0o755)
    return native


def test_stage_creates_public_names_and_checksummed_archive(tmp_path):
    app_dir = make_app(tmp_path)
    native = make_native(tmp_path)
    output = tmp_path / "release"
    archive, checksum = stager.stage_native_release(
        app_dir, native, output, "0.11.0", "darwin_arm64"
    )
    root = output / "jarvis"
    assert archive.name == "jarvis_0.11.0_darwin_arm64.tar.gz"
    assert checksum.name == archive.name + ".sha256"
    assert (root / "libexec" / "jarvis").is_file()
    assert os.path.islink(root / "bin" / "jarvis")
    assert os.path.islink(root / "bin" / "jarvis-server")
    assert (root / "libexec" / "_internal" / "native-bin" / "scip").is_file()
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_text().split()[0] == expected
    with tarfile.open(archive, "r:gz") as tar:
        assert "jarvis/bin/jarvis-server" in tar.getnames()


def test_stage_rejects_missing_launcher(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    native = make_native(tmp_path)
    try:
        stager.stage_native_release(
            app_dir, native, tmp_path / "out", "0.11.0", "linux_amd64"
        )
    except RuntimeError as exc:
        assert "app/jarvis/jarvis" in str(exc)
    else:
        raise AssertionError("missing launcher must fail")
