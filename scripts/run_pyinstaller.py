"""Invoke PyInstaller through its stable Python API."""

from __future__ import annotations

import os
import sys

from PyInstaller.__main__ import run as run_pyinstaller


def main() -> int:
    output = os.environ.get("JARVIS_APP_OUTPUT_DIR")
    work = os.environ.get("JARVIS_PYINSTALLER_WORK_DIR")
    if not output or not work:
        print(
            "error: JARVIS_APP_OUTPUT_DIR and "
            "JARVIS_PYINSTALLER_WORK_DIR are required",
            file=sys.stderr,
        )
        return 2
    run_pyinstaller([
        "packaging/jarvis.spec", "--noconfirm",
        "--workpath", work, "--distpath", output,
    ])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
