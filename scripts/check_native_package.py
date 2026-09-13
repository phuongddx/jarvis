"""Validate the contents and architectures of a standalone jarvis archive."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

GRAMMARS = (
    "python", "javascript", "typescript", "java", "kotlin", "swift", "go",
    "ruby", "rust", "c", "cpp", "c_sharp", "php", "scala", "bash", "sql",
)
REQUIRED_NATIVE = ("scip", "zoekt-git-index", "zoekt-webserver")
REQUIRED_MODULES = ("query", "syntax", "index_cli", "server", "dashboard")
REQUIRED_ASSETS = ("index.html", "app.js", "style.css")
FORBIDDEN_SEMANTIC = ("lancedb", "sentence_transformers", "torch")
FORBIDDEN_PACKAGE_MANAGERS = ("uv", "uvx", "pip", "pip3")
FORBIDDEN_LANGUAGE_INDEXERS = (
    "scip-python", "scip-typescript", "scip-java", "scip-swift",
)
SOURCE_EXCEPTIONS = {"__init__.py", "scip_pb2.py"}
ARCH_PATTERNS = {
    "darwin_arm64": ("Mach-O", "arm64"),
    "darwin_amd64": ("Mach-O", "x86_64"),
    "linux_arm64": ("ELF", "aarch64"),
    "linux_amd64": ("ELF", "x86_64"),
}


def is_executable(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o111 != 0


def file_type(path: Path) -> str:
    result = subprocess.run(
        ["file", str(path)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _architecture_problems(
    binaries: list[Path], platform: str, file_types: dict[Path, str] | None
) -> list[str]:
    expected_format, expected_arch = ARCH_PATTERNS[platform]
    descriptions: dict[Path, str] = {}
    for path in binaries:
        if not is_executable(path):
            continue
        if file_types is not None and path in file_types:
            descriptions[path] = file_types[path]
        else:
            descriptions[path] = file_type(path)

    problems: list[str] = []
    mismatches: list[Path] = []
    for path, description in descriptions.items():
        normalized = description.replace("x86-64", "x86_64")
        if expected_format not in normalized or expected_arch not in normalized:
            problems.append(
                f"wrong architecture for {platform}: {path}: {description!r}"
            )
            mismatches.append(path)
    if mismatches and len(mismatches) != len(descriptions):
        problems.append(f"mixed architecture package: {', '.join(map(str, mismatches))}")
    return problems


def _bundled_paths(root: Path, names: tuple[str, ...]) -> dict[str, list[Path]]:
    return {
        name: [
            path
            for path in root.rglob(name)
            if path.exists() or path.is_symlink()
        ]
        for name in names
    }


def validate(
    root: Path, platform: str, file_types: dict[Path, str] | None = None
) -> list[str]:
    problems: list[str] = []
    if platform not in ARCH_PATTERNS:
        return [f"unsupported platform: {platform!r}"]

    internal = root / "libexec" / "_internal"
    jarvis = internal / "jarvis"
    assets = jarvis / "dashboard_assets"
    native_bin = internal / "native-bin"

    launcher = root / "libexec" / "jarvis"
    if not is_executable(launcher):
        problems.append(f"missing executable launcher: {launcher}")

    binaries = [launcher]
    for name in REQUIRED_NATIVE:
        path = native_bin / name
        binaries.append(path)
        if not is_executable(path):
            problems.append(f"missing executable native binary: {name}: {path}")

    for name in REQUIRED_ASSETS:
        if not (assets / name).is_file():
            problems.append(f"missing dashboard asset: {name}")
    for name in REQUIRED_MODULES:
        if not any(jarvis.glob(f"{name}.cpython-*.so")):
            problems.append(f"missing compiled module: {name}")
    for grammar in GRAMMARS:
        stem = f"tree_sitter_{grammar}"
        # Boundary-safe matching: tree_sitter_java must not be satisfied by
        # tree_sitter_javascript. Current wheels ship as packages containing
        # a binding extension; older wheels shipped a single flat extension.
        flat = [
            path
            for path in internal.glob(f"{stem}*.so")
            if path.name == f"{stem}.so" or path.name.startswith(f"{stem}.")
        ]
        if not (flat or any((internal / stem).glob("_binding*.so"))):
            problems.append(f"missing tree-sitter library: {grammar}")

    for source in sorted(jarvis.rglob("*.py")):
        relative = source.relative_to(jarvis)
        if len(relative.parts) == 1 and relative.name in SOURCE_EXCEPTIONS:
            continue
        problems.append(f"readable jarvis source: {relative}")

    for name in FORBIDDEN_SEMANTIC:
        if any(path.name.startswith(name) for path in root.rglob("*")):
            problems.append(f"bundled semantic dependency: {name}")

    managers = _bundled_paths(root, FORBIDDEN_PACKAGE_MANAGERS)
    for name, paths in managers.items():
        if any(is_executable(path) for path in paths):
            problems.append(f"bundled package manager executable: {name}")

    indexers = _bundled_paths(root, FORBIDDEN_LANGUAGE_INDEXERS)
    for name, paths in indexers.items():
        if paths:
            problems.append(f"bundled language indexer: {name}: {paths[0]}")

    problems.extend(_architecture_problems(binaries, platform, file_types))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args(argv)

    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-native-check-") as temporary:
            extracted = Path(temporary)
            with tarfile.open(args.archive, "r:gz") as tar:
                tar.extractall(extracted, filter="data")
            problems = validate(extracted / "jarvis", args.platform)
    except (OSError, tarfile.TarError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print("native package: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
