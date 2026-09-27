# Infrastructure

## Runtime

- Python `>=3.12,<3.15` (`pyproject.toml`); `.python-version` pins 3.12.
- Use `uv` exclusively. Override CI interpreters with `UV_PYTHON`, never `uv sync --python`.
- Local state defaults to `~/.jarvis`; override with `JARVIS_DATA_DIR` for persistent-state work.
- Native pins: `SCIP_COMMIT` (`56791658a873`) and `ZOEKT_COMMIT` (`33f1f18af292`); binaries fetched by `scripts/fetch_native_binaries.py` from `jarvis-intelligence/jarvis-index` releases.

## Commands

```sh
uv sync                                  # base install
uv sync --extra semantic --group native  # CI parity
uv run pytest -m "not integration" -rs   # unit CI gate
uv run pytest -m integration -rs         # real binaries; skips if absent
uv run python scripts/check_versions.py
uv run jarvis index /path/to/repo
uv run jarvis-server
```

## CI

- `.github/workflows/test.yml` — push/PR/dispatch; Ubuntu Python 3.12/3.13 + macOS 3.13; CI-parity install, unit gate, version check. No lint step.
- `.github/workflows/setup-smoke.yml` — `sh -n setup.sh` + `tests/test_setup_sh.py` on Ubuntu/macOS.
- `.github/workflows/build-scip.yml` / `build-zoekt.yml` — rebuild pinned binaries when the commit files change; publish checksummed tarballs to `jarvis-intelligence/jarvis-index`.
- `.github/workflows/publish-native.yml` — on GitHub Release (or manual build-only dispatch): four-platform Cython wheel → PyInstaller → archive/smoke → generated Homebrew formula to `jarvis-intelligence/homebrew-jarvis`.

## Deploy / release target

- Homebrew standalone distribution only: `brew install jarvis-intelligence/jarvis/jarvis`.
- No PyPI or environment deployment is wired. Tag `vX.Y.Z` must match `pyproject.toml` version.
- Release runbook: `.claude/skills/jarvis-release/SKILL.md`.

## Optional indexer bootstrap

`setup.sh --only scip-swift|scip-typescript|scip-python|scip-java|bash-shim` can mutate home/shell state; isolate with `JARVIS_BIN_DIR` and `JARVIS_DATA_DIR`.
