"""Tests for standalone archive validation."""

from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_native_package.py"
spec = importlib.util.spec_from_file_location("check_native_package", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def valid_tree(parent: Path) -> Path:
    root = parent / "jarvis"
    internal = root / "libexec" / "_internal"
    jarvis = internal / "jarvis"
    native = internal / "native-bin"
    assets = jarvis / "dashboard_assets"
    jarvis.mkdir(parents=True)
    native.mkdir()
    assets.mkdir()
    for name in ("index.html", "app.js", "style.css"):
        (assets / name).write_text(name)
    for name in ("query", "syntax", "index_cli", "server", "dashboard"):
        (jarvis / f"{name}.cpython-312-darwin.so").write_bytes(b"compiled")
    for name in checker.REQUIRED_NATIVE:
        path = native / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    for grammar in checker.GRAMMARS:
        (internal / f"tree_sitter_{grammar}.so").write_bytes(b"grammar")
    (jarvis / "__init__.py").write_text("")
    (jarvis / "scip_pb2.py").write_text("# vendored")
    launcher = root / "libexec" / "jarvis"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    return root


def binary_paths(root: Path) -> tuple[Path, ...]:
    return (
        root / "libexec" / "jarvis",
        *(
            root / "libexec" / "_internal" / "native-bin" / name
            for name in checker.REQUIRED_NATIVE
        ),
    )


def injected_types(root: Path, architecture: str = "arm64") -> dict[Path, str]:
    return {path: f"Mach-O {architecture}" for path in binary_paths(root)}


def test_valid_tree_passes_with_injected_file_types(tmp_path):
    root = valid_tree(tmp_path)
    assert checker.validate(root, "darwin_arm64", injected_types(root)) == []


def test_linux_amd64_accepts_gnu_hyphenated_file_description(tmp_path):
    root = valid_tree(tmp_path)
    types = {
        path: "ELF 64-bit LSB pie executable, x86-64, dynamically linked"
        for path in binary_paths(root)
    }
    problems = checker.validate(root, "linux_amd64", types)
    assert [problem for problem in problems if "architecture" in problem] == []


def test_rejects_unsupported_platform(tmp_path):
    root = valid_tree(tmp_path)
    problems = checker.validate(root, "windows_amd64", injected_types(root))
    assert problems == ["unsupported platform: 'windows_amd64'"]


def test_rejects_missing_launcher(tmp_path):
    root = valid_tree(tmp_path)
    (root / "libexec" / "jarvis").unlink()
    problems = checker.validate(root, "darwin_arm64", {})
    assert any("missing executable launcher" in problem for problem in problems)


def test_rejects_missing_native_binary_and_wrong_launcher_architecture(tmp_path):
    root = valid_tree(tmp_path)
    (root / "libexec" / "_internal" / "native-bin" / "scip").unlink()
    types = injected_types(root, "x86_64")
    del types[root / "libexec" / "_internal" / "native-bin" / "scip"]
    problems = checker.validate(root, "darwin_arm64", types)
    assert any("missing executable native binary: scip" in p for p in problems)
    assert any("wrong architecture" in problem for problem in problems)


@pytest.mark.parametrize("binary_name", ["jarvis", *checker.REQUIRED_NATIVE])
def test_rejects_wrong_architecture_for_every_required_binary(
    tmp_path, binary_name
):
    root = valid_tree(tmp_path)
    binary = next(path for path in binary_paths(root) if path.name == binary_name)
    types = injected_types(root)
    types[binary] = "Mach-O x86_64"
    problems = checker.validate(root, "darwin_arm64", types)
    assert any(
        f"wrong architecture for darwin_arm64: {binary}"
        in problem
        for problem in problems
    )
    assert any("mixed architecture" in problem for problem in problems)


def test_rejects_missing_dashboard_asset(tmp_path):
    root = valid_tree(tmp_path)
    (root / "libexec" / "_internal" / "jarvis" / "dashboard_assets" / "app.js").unlink()
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    assert any("missing dashboard asset: app.js" in problem for problem in problems)


def test_rejects_missing_compiled_module(tmp_path):
    root = valid_tree(tmp_path)
    jarvis = root / "libexec" / "_internal" / "jarvis"
    next(jarvis.glob("query.cpython-*.so")).unlink()
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    assert any("missing compiled module: query" in problem for problem in problems)


def test_rejects_missing_tree_sitter_grammar(tmp_path):
    root = valid_tree(tmp_path)
    internal = root / "libexec" / "_internal"
    next(internal.glob("tree_sitter_kotlin*.so")).unlink()
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    assert any("missing tree-sitter library: kotlin" in problem for problem in problems)


def test_rejects_readable_jarvis_source(tmp_path):
    root = valid_tree(tmp_path)
    source = root / "libexec" / "_internal" / "jarvis" / "query.py"
    source.write_text("def f(): pass")
    problems = checker.validate(root, "darwin_arm64", {})
    assert any("readable jarvis source" in problem for problem in problems)


def test_source_protection_allows_only_the_two_root_exceptions(tmp_path):
    root = valid_tree(tmp_path)
    jarvis = root / "libexec" / "_internal" / "jarvis"
    nested = jarvis / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text("")
    problems = checker.validate(root, "darwin_arm64", {})
    assert any("nested/__init__.py" in problem for problem in problems)


def test_rejects_semantic_dependency(tmp_path):
    root = valid_tree(tmp_path)
    semantic = root / "libexec" / "_internal" / "lancedb"
    semantic.mkdir()
    (semantic / "__init__.py").write_text("")
    problems = checker.validate(root, "darwin_arm64", {})
    assert any("semantic dependency: lancedb" in problem for problem in problems)


def test_rejects_semantic_dependency_outside_internal(tmp_path):
    root = valid_tree(tmp_path)
    semantic = root / "lancedb"
    semantic.mkdir()
    (semantic / "__init__.py").write_text("")
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    assert any("semantic dependency: lancedb" in problem for problem in problems)


def test_rejects_bundled_package_manager_executable(tmp_path):
    root = valid_tree(tmp_path)
    uv = root / "libexec" / "_internal" / "native-bin" / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    assert any(
        "bundled package manager executable: uv" in problem for problem in problems
    )


def test_rejects_each_bundled_language_indexer(tmp_path):
    assert checker.FORBIDDEN_LANGUAGE_INDEXERS == (
        "scip-python", "scip-typescript", "scip-java", "scip-swift",
    )
    root = valid_tree(tmp_path)
    native = root / "libexec" / "_internal" / "native-bin"
    for name in checker.FORBIDDEN_LANGUAGE_INDEXERS:
        indexer = native / name
        indexer.write_text("#!/bin/sh\n")
        indexer.chmod(0o755)
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    for name in checker.FORBIDDEN_LANGUAGE_INDEXERS:
        assert any(f"bundled language indexer: {name}" in p for p in problems)


def test_cli_extracts_and_validates_archive(tmp_path, monkeypatch, capsys):
    root = valid_tree(tmp_path)
    archive = tmp_path / "jarvis.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname="jarvis")

    def fake_file_type(path: Path) -> str:
        if Path(path).name in {
            "jarvis", *checker.REQUIRED_NATIVE,
        }:
            return "Mach-O arm64"
        return "script"

    monkeypatch.setattr(checker, "file_type", fake_file_type)
    assert checker.main([str(archive), "--platform", "darwin_arm64"]) == 0
    assert capsys.readouterr().out == "native package: PASS\n"


def test_cli_reports_validation_problem(tmp_path, monkeypatch, capsys):
    root = valid_tree(tmp_path)
    (root / "libexec" / "jarvis").unlink()
    archive = tmp_path / "jarvis.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(root, arcname="jarvis")
    monkeypatch.setattr(checker, "file_type", lambda _path: "Mach-O arm64")
    assert checker.main([str(archive), "--platform", "darwin_arm64"]) == 1
    assert "missing executable launcher" in capsys.readouterr().err


def test_accepts_tree_sitter_package_layout(tmp_path):
    root = valid_tree(tmp_path)
    internal = root / "libexec" / "_internal"
    for grammar in checker.GRAMMARS:
        (internal / f"tree_sitter_{grammar}.so").unlink()
        package = internal / f"tree_sitter_{grammar}"
        package.mkdir()
        (package / "_binding.abi3.so").write_bytes(b"grammar")
    assert checker.validate(root, "darwin_arm64", injected_types(root)) == []


def test_rejects_missing_java_even_when_javascript_remains(tmp_path):
    root = valid_tree(tmp_path)
    internal = root / "libexec" / "_internal"
    (internal / "tree_sitter_java.so").unlink()
    problems = checker.validate(root, "darwin_arm64", injected_types(root))
    assert "missing tree-sitter library: java" in problems
    assert "missing tree-sitter library: javascript" not in problems


def test_accepts_any_binding_extension_in_package_layout(tmp_path):
    root = valid_tree(tmp_path)
    internal = root / "libexec" / "_internal"
    for grammar in checker.GRAMMARS:
        (internal / f"tree_sitter_{grammar}.so").unlink()
        package = internal / f"tree_sitter_{grammar}"
        package.mkdir()
        (package / "_binding.cpython-312-darwin.so").write_bytes(b"grammar")
    assert checker.validate(root, "darwin_arm64", injected_types(root)) == []
