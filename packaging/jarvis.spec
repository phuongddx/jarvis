# -*- mode: python ; coding: utf-8 -*-
"""Collection rules for jarvis's standalone distribution."""
from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import os
from pathlib import Path

native_bin = Path(os.environ["JARVIS_NATIVE_BIN_DIR"])
jarvis_spec = importlib.util.find_spec("jarvis")
if jarvis_spec is None or jarvis_spec.submodule_search_locations is None:
    raise RuntimeError("install the compiled wheel before running PyInstaller")
package_root = Path(next(iter(jarvis_spec.submodule_search_locations)))
launcher = Path(SPECPATH) / "launcher.py"
grammars = (
    "python", "javascript", "typescript", "java", "kotlin", "swift", "go",
    "ruby", "rust", "c", "cpp", "c_sharp", "php", "scala", "bash", "sql",
)
# The wheel is Cython-compiled, so PyInstaller cannot trace imports between
# jarvis modules or from them to stdlib/third-party dependencies; everything
# must be requested explicitly. Compiled module names come from the installed
# wheel, while their imports come from the sources next to this spec.
compiled_modules = sorted(
    path.name.split(".", 1)[0]
    for path in package_root.glob("*.cpython-*.so")
)
excludes = ("lancedb", "sentence_transformers", "torch")
source_root = Path(SPECPATH).parent / "src" / "jarvis"
source_imports: set[str] = set()
for source in source_root.glob("*.py"):
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.Import):
            source_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            source_imports.add(node.module)
source_imports = {
    name
    for name in source_imports
    if name.split(".", 1)[0] not in {"jarvis", "__future__", *excludes}
}
hiddenimports = sorted(
    {f"jarvis.{name}" for name in compiled_modules}
    | {"jarvis.scip_pb2"}
    | source_imports
    | {
        "mcp.server.fastmcp", "mcp.server.stdio",
        "watchdog", "watchdog.observers",
    }
    | {f"tree_sitter_{name}" for name in grammars}
)
binaries = [
    (str(native_bin / name), "native-bin")
    for name in ("scip", "zoekt-git-index", "zoekt-webserver")
]
datas = [(str(package_root / "dashboard_assets"), "jarvis/dashboard_assets")]
# grammar_identity()/ParserPool read distribution versions through
# importlib.metadata at runtime, so each grammar's dist-info must ship too.
metadata_names = [
    "tree-sitter",
    *sorted(f"tree-sitter-{name.replace('_', '-')}" for name in grammars),
]
for metadata_name in metadata_names:
    dist_info = Path(importlib.metadata.distribution(metadata_name)._path)
    datas.append((str(dist_info), dist_info.name))

a = Analysis(
    [str(launcher)], pathex=[], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[], excludes=excludes,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="jarvis", console=True)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="jarvis")
