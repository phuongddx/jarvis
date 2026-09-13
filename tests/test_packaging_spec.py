"""Tests for the PyInstaller spec's PyInstaller-provided globals."""

from __future__ import annotations

import importlib.metadata
import importlib.util
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


def test_hiddenimports_cover_every_compiled_module(monkeypatch, tmp_path):
    package_root = tmp_path / "site-packages" / "jarvis"
    package_root.mkdir(parents=True)
    expected = ("query", "syntax", "index_cli", "server", "dashboard")
    for name in expected:
        (package_root / f"{name}.cpython-312-darwin.so").write_bytes(b"")
    (package_root / "__init__.py").write_text("")
    (package_root / "scip_pb2.py").write_text("")
    fake_spec = SimpleNamespace(submodule_search_locations=[str(package_root)])
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: fake_spec if name == "jarvis" else None,
    )
    monkeypatch.setenv("JARVIS_NATIVE_BIN_DIR", str(tmp_path / "native"))
    namespace: dict[str, object] = {"SPECPATH": str(SPEC.parent), **stub_api()}
    exec(compile(SPEC.read_bytes(), str(SPEC), "exec"), namespace)
    hidden = namespace["hiddenimports"]
    for name in expected:
        assert f"jarvis.{name}" in hidden
    assert "jarvis.scip_pb2" in hidden


def test_hiddenimports_cover_imports_from_jarvis_sources(monkeypatch, tmp_path):
    source_root = tmp_path / "src" / "jarvis"
    source_root.mkdir(parents=True)
    (source_root / "index_cli.py").write_text(
        "import sqlite3\nimport zstandard\nfrom jarvis import config\n"
    )
    package_root = tmp_path / "site-packages" / "jarvis"
    package_root.mkdir(parents=True)
    (package_root / "index_cli.cpython-312-darwin.so").write_bytes(b"")
    spec_dir = tmp_path / "packaging"
    spec_dir.mkdir()
    (spec_dir / "launcher.py").write_text("")
    fake_spec = SimpleNamespace(submodule_search_locations=[str(package_root)])
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: fake_spec if name == "jarvis" else None,
    )
    monkeypatch.setenv("JARVIS_NATIVE_BIN_DIR", str(tmp_path / "native"))
    namespace: dict[str, object] = {"SPECPATH": str(spec_dir), **stub_api()}
    exec(compile(SPEC.read_bytes(), str(SPEC), "exec"), namespace)
    hidden = namespace["hiddenimports"]
    assert "sqlite3" in hidden
    assert "zstandard" in hidden


def fake_distribution(tmp_path):
    def distribution(name: str) -> SimpleNamespace:
        stem = name.replace("-", "_")
        return SimpleNamespace(_path=str(tmp_path / f"{stem}-1.2.3.dist-info"))

    return distribution


def test_datas_include_grammar_distribution_metadata(monkeypatch, tmp_path):
    package_root = tmp_path / "site-packages" / "jarvis"
    package_root.mkdir(parents=True)
    fake_spec = SimpleNamespace(submodule_search_locations=[str(package_root)])
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: fake_spec if name == "jarvis" else None,
    )
    monkeypatch.setattr(
        importlib.metadata, "distribution", fake_distribution(tmp_path)
    )
    monkeypatch.setenv("JARVIS_NATIVE_BIN_DIR", str(tmp_path / "native"))
    namespace: dict[str, object] = {"SPECPATH": str(SPEC.parent), **stub_api()}
    exec(compile(SPEC.read_bytes(), str(SPEC), "exec"), namespace)
    dests = {Path(dest).name for _, dest in namespace["datas"]}
    assert "tree_sitter-1.2.3.dist-info" in dests
    assert "tree_sitter_python-1.2.3.dist-info" in dests
    assert "tree_sitter_c_sharp-1.2.3.dist-info" in dests
