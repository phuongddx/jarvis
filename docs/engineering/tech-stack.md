# Tech stack

## Language and package management

- Python 3.12+ (`.python-version` pins 3.12); setuptools build backend, `src/` layout.
- `uv` for all dependency work; regenerate `uv.lock` after version bumps, never hand-edit it.
- Optional Cython compilation of `src/jarvis/*.py` under `JARVIS_COMPILE=1` (`setup.py`); `__init__.py` and generated `scip_pb2.py` stay plain source.

## Core dependencies

- FastMCP — stdio MCP server (`src/jarvis/server.py`).
- Tree-sitter grammar packages — base dependencies for the syntax baseline.
- SCIP — pinned binaries via `SCIP_COMMIT`; protobuf decode isolated in `src/jarvis/scip_decoder.py`.
- Zoekt — pinned `zoekt-git-index` / `zoekt-webserver` via `ZOEKT_COMMIT`.
- Optional extras: `semantic` = `lancedb` + `sentence-transformers`; `watch` = `watchdog`.
- Dependency groups: `dev` = `pytest>=8.3`; `native` = `pyinstaller==6.14.1`.

## Testing

- pytest with markers `integration` (real binaries) and `interactive_input`; async MCP tests use `anyio` in-memory sessions (`tests/test_server_tools.py`).

## Distribution

- Distribution `jarvis-mcp`; executables `jarvis` (`jarvis.index_cli:main`) and `jarvis-server` (`jarvis.server:main`).
- Native release path: Cython wheel → PyInstaller (`packaging/jarvis.spec`, excluding semantic deps) → Homebrew formula. Semantic search is source-build only in Homebrew installs.
