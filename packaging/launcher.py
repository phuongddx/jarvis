"""Single PyInstaller entrypoint for jarvis and jarvis-server."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis import runtime


def select_entrypoint(program: str) -> str:
    name = Path(program).name
    if name == "jarvis":
        return "cli"
    if name == "jarvis-server":
        return "server"
    print(
        f"error: unsupported launcher name {name!r} "
        "(expected jarvis or jarvis-server)",
        file=sys.stderr,
    )
    raise SystemExit(2)


def run(
    cli_main: Callable[[], Any] | None = None,
    server_main: Callable[[], Any] | None = None,
    runtime_initialize: Callable[..., Any] = runtime.initialize,
) -> int | None:
    runtime_initialize()
    mode = select_entrypoint(sys.argv[0])
    if mode == "server":
        if server_main is None:
            from jarvis.server import main as server_main
        server_main()
        return None
    if cli_main is None:
        from jarvis.index_cli import main as cli_main
    return int(cli_main())


if __name__ == "__main__":
    raise SystemExit(run())
