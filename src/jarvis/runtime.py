"""Runtime helpers for wheel and frozen PyInstaller distributions."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return True when running from the PyInstaller launcher."""
    return bool(getattr(sys, "frozen", False))


def bundled_bin_dir(base: Path | None = None) -> Path | None:
    """Return the native-bin directory.

    An explicit ``base`` is treated as the already-existing native-bin
    directory. With no ``base``, frozen mode derives it from
    ``sys._MEIPASS/native-bin`` or the executable's adjacent
    ``_internal/native-bin`` directory.
    """
    if base is None:
        if not is_frozen():
            return None
        raw = getattr(sys, "_MEIPASS", None)
        if raw is None:
            raw = Path(sys.executable).resolve().parent / "_internal"
        candidate = Path(raw) / "native-bin"
    else:
        candidate = base
    try:
        candidate = candidate.resolve(strict=True)
    except OSError:
        return None
    return candidate if candidate.is_dir() else None


def initialize(base: Path | None = None) -> Path | None:
    """Expose embedded binaries to subprocess PATH lookup.

    Explicit environment overrides such as ``JARVIS_ZOEKT_BIN`` are
    deliberately untouched.
    """
    native = bundled_bin_dir(base)
    if native is None:
        return None
    entries = os.environ.get("PATH", "").split(os.pathsep)
    target = str(native)
    if target not in entries:
        os.environ["PATH"] = os.pathsep.join([target, *entries])
    return native
