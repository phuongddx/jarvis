# Conventions

## Code style

- Python 3.12+ syntax: `X | None`, builtin generics, frozen dataclasses, synchronous flow.
- SQLite access uses parameterized raw SQL (see `src/jarvis/registry.py`, `src/jarvis/graph.py`).
- Keep `scip_pb2`/`zstandard` isolated in `src/jarvis/scip_decoder.py`.
- Resolve `JARVIS_*` environment variables centrally (`src/jarvis/config.py`); import optional extras lazily at use sites.
- Typed exceptions belong in library code, not at MCP/dashboard boundaries — boundaries return structured `{"error": ...}` payloads (`src/jarvis/server.py`, `src/jarvis/dashboard.py`).
- Keep stdout machine-parseable; never print incidental diagnostics to it.
- No lint/format/type-check tool is configured — `Not configured`.

## Naming

- Python modules/packages: `snake_case`; tests mirror modules as `tests/test_<module>.py`.
- Commit scopes follow existing usage: `cli`, `semantic`, `query`, `readme`, etc.

## Commits

- Lowercase imperative Conventional Commits: `feat(cli): pre-download embedding model in install-semantic`, `fix(semantic): guide homebrew users to source-build setup`, `docs(readme): document model pre-download and homebrew guidance`.
- Release commits bundle `CHANGELOG.md`, `pyproject.toml`, `uv.lock` as `chore: release X.Y.Z`.

## Pull requests

- Target `main`; keep `uv run pytest -m "not integration" -rs` green.
- Unit tests mock subprocess, HTTP, MCP, Zoekt, and embedding boundaries; real binaries only in `integration`-marked tests.
- State-touching tests must set `JARVIS_DATA_DIR` to `tmp_path`.
