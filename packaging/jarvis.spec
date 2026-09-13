# -*- mode: python ; coding: utf-8 -*-
"""Collection rules for jarvis's standalone distribution."""
from __future__ import annotations

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
hiddenimports = (
    [
        "jarvis.index_cli", "jarvis.server", "jarvis.dashboard",
        "mcp.server.fastmcp", "mcp.server.stdio",
        "watchdog", "watchdog.observers",
    ]
    + [f"tree_sitter_{name}" for name in grammars]
)
binaries = [
    (str(native_bin / name), "native-bin")
    for name in ("scip", "zoekt-git-index", "zoekt-webserver")
]
datas = [(str(package_root / "dashboard_assets"), "jarvis/dashboard_assets")]
excludes = ["lancedb", "sentence_transformers", "torch"]

a = Analysis(
    [str(launcher)], pathex=[], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], runtime_hooks=[], excludes=excludes,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="jarvis", console=True)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="jarvis")
