"""Tests for the PyInstaller spec's PyInstaller-provided globals."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

SPEC = Path(__file__).resolve().parents[1] / "packaging" / "jarvis.spec"


def stub_api() -> dict[str, object]:
    """Mirror the spec API PyInstaller injects, without executing a build."""
    analysis_result = SimpleNamespace(
        pure=None, scripts=None, binaries=None, zipfiles=None, datas=None
    )

    def analysis(*args: object, **kwargs: object) -> SimpleNamespace:
        return analysis_result

    def passthrough(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace()

    return {
        "Analysis": analysis,
        "PYZ": passthrough,
        "EXE": passthrough,
        "COLLECT": passthrough,
    }


def test_launcher_resolves_from_specpath_without_dunder_file(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_NATIVE_BIN_DIR", str(tmp_path))
    namespace: dict[str, object] = {"SPECPATH": str(SPEC.parent), **stub_api()}
    assert "__file__" not in namespace
    code = compile(SPEC.read_bytes(), str(SPEC), "exec")
    exec(code, namespace)
    assert Path(namespace["launcher"]) == SPEC.parent / "launcher.py"
