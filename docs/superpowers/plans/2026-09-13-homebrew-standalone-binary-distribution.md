# Homebrew Standalone Binary Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the end-user uv/PyPI install with checksummed PyInstaller archives for four platforms, distributed and installed through a custom Homebrew tap.

**Architecture:** Add a frozen-runtime seam and argv-based launcher, then package the existing Cython-compiled application with Python 3.12, `watch`, dashboard assets, tree-sitter libraries, and pinned SCIP/Zoekt binaries. CI builds and validates four archives, generates a Homebrew formula, validates it, then commits it to the public tap.

**Tech Stack:** Python 3.12, Cython-compiled setuptools wheel, PyInstaller 6.14.1, tar/SHA-256, GitHub Actions, manylinux 2.28, Homebrew formulas.

**Spec:** [`docs/superpowers/specs/2026-09-13-homebrew-standalone-binary-distribution-design.md`](../specs/2026-09-13-homebrew-standalone-binary-distribution-design.md)

## Global Constraints

- Build exactly `darwin_arm64`, `darwin_amd64`, `linux_arm64`, and `linux_amd64`; all four must pass before the formula is committed.
- End-user install is exactly `brew install jarvis-intelligence/jarvis/jarvis`.
- End users need no Python, uv, pip, or PyPI.
- Bundle only `scip`, `zoekt-git-index`, and `zoekt-webserver`; language indexers remain PATH-discovered extras.
- Include `watch`; exclude `semantic`, `lancedb`, `sentence_transformers`, and `torch`.
- Keep repo-root `SCIP_COMMIT` and `ZOEKT_COMMIT`; keep `build-scip.yml` and `build-zoekt.yml`.
- No migration command or compatibility layer.
- Formula URLs point only to public releases in `jarvis-intelligence/homebrew-jarvis`.
- Development remains uv-based; the normal gate is `uv run pytest -m "not integration" -rs`.
- New index/registry tests set `JARVIS_DATA_DIR` to `tmp_path`.
- Never edit `src/jarvis/scip_pb2.py`.
- Native artifacts may contain readable jarvis source only for `jarvis/__init__.py` and `jarvis/scip_pb2.py`.
- Do not remove legacy workflows until Task 11; earlier tasks must keep the repository testable.
- Use Conventional Commits with lowercase imperative subjects.

---

### Task 1: Frozen runtime seam

**Files:**
- Create: `src/jarvis/runtime.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Produces: `is_frozen() -> bool`
- Produces: `bundled_bin_dir(base: Path | None = None) -> Path | None`
- Produces: `initialize(base: Path | None = None) -> Path | None`
- Later tasks use `is_frozen()` for semantic behavior and `initialize()` for bundled-binary PATH precedence.

- [ ] **Step 1: Write failing tests**

Create `tests/test_runtime.py`:

```python
"""Tests for the frozen PyInstaller runtime seam."""

from __future__ import annotations

import os
from pathlib import Path

from jarvis import runtime


def test_development_mode_has_no_bundled_bin_dir(monkeypatch):
    monkeypatch.setattr(runtime.sys, "frozen", False, raising=False)
    assert runtime.is_frozen() is False
    assert runtime.bundled_bin_dir(Path("/does/not/exist")) is None


def test_frozen_mode_resolves_internal_native_bin(monkeypatch, tmp_path):
    native = tmp_path / "_internal" / "native-bin"
    native.mkdir(parents=True)
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "_MEIPASS", tmp_path / "_internal", raising=False)
    assert runtime.is_frozen() is True
    assert runtime.bundled_bin_dir() == native


def test_missing_frozen_directory_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime.sys, "_MEIPASS", tmp_path, raising=False)
    assert runtime.bundled_bin_dir() is None


def test_initialize_prepends_bundled_bin_exactly_once(monkeypatch, tmp_path):
    native = tmp_path / "native-bin"
    native.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("PATH", str(other))
    assert runtime.initialize(base=native) == native
    assert os.environ["PATH"] == os.pathsep.join([str(native), str(other)])
    runtime.initialize(base=native)
    assert os.environ["PATH"] == os.pathsep.join([str(native), str(other)])


def test_initialize_does_not_override_jarvis_zoekt_bin(monkeypatch, tmp_path):
    native = tmp_path / "native-bin"
    native.mkdir()
    override = tmp_path / "custom-zoekt"
    monkeypatch.setenv("JARVIS_ZOEKT_BIN", str(override))
    assert runtime.initialize(base=native) == native
    assert os.environ["JARVIS_ZOEKT_BIN"] == str(override)


def test_initialize_returns_none_in_development(monkeypatch):
    monkeypatch.delenv("JARVIS_ZOEKT_BIN", raising=False)
    assert runtime.initialize(base=None) is None
```

- [ ] **Step 2: Verify the test fails**

Run: `uv run pytest tests/test_runtime.py -q`  
Expected: FAIL with `ModuleNotFoundError: No module named 'jarvis.runtime'`.

- [ ] **Step 3: Implement the runtime seam**

Create `src/jarvis/runtime.py`:

```python
"""Runtime helpers for wheel and frozen PyInstaller distributions."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """Return True when running from the PyInstaller launcher."""
    return bool(getattr(sys, "frozen", False))


def bundled_bin_dir(base: Path | None = None) -> Path | None:
    """Return the embedded native-bin directory when it exists."""
    if base is None:
        if not is_frozen():
            return None
        raw = getattr(sys, "_MEIPASS", None)
        if raw is None:
            raw = Path(sys.executable).resolve().parent / "_internal"
        base = Path(raw)
    try:
        candidate = (base / "native-bin").resolve(strict=True)
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
```

`Path.resolve(strict=True)` raises `OSError`, not `FileNotFoundError`, for all unsupported hosts, so no caller needs to catch a narrower exception.

- [ ] **Step 4: Verify tests pass**

Run: `uv run pytest tests/test_runtime.py -q`  
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/runtime.py tests/test_runtime.py
git commit -m "feat(runtime): add frozen binary discovery"
```

---

### Task 2: Version flag and frozen semantic behavior

**Files:**
- Modify: `src/jarvis/index_cli.py`
- Modify: `src/jarvis/embeddings.py`
- Modify: `src/jarvis/semantic.py`
- Test: `tests/test_index_cli.py`

**Interfaces:**
- Consumes: `jarvis.runtime.is_frozen()`
- Produces: top-level `jarvis --version`
- Produces: frozen semantic paths that never call `_install_semantic_extra()`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_index_cli.py`:

```python
def test_version_flag_reports_distribution_version(capsys):
    from jarvis import index_cli as cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert raised.value.code == 0
    assert "jarvis " in capsys.readouterr().out


def test_semantic_offer_is_structurally_disabled_in_frozen_build(monkeypatch, tmp_path):
    from jarvis import index_cli as cli
    from jarvis import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    monkeypatch.setattr(cli, "index_repo", lambda *args, **kwargs: "frozen")
    monkeypatch.setattr(
        cli,
        "_semantic_extra_missing",
        lambda: (_ for _ in ()).throw(AssertionError("frozen build must not offer")),
    )
    monkeypatch.setattr(
        cli,
        "_at_interactive_tty",
        lambda: (_ for _ in ()).throw(AssertionError("frozen build must not prompt")),
    )
    args = argparse.Namespace(path=str(tmp_path), slug="frozen", offer_semantic=True)
    assert cli._cmd_index(args) == 0


def test_frozen_semantic_skip_names_homebrew_distribution(monkeypatch, tmp_path, capsys):
    from jarvis import index_cli as cli
    from jarvis import runtime

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    assert cli._prepare_semantic_stage(tmp_path, "frozen", tmp_path, (), None) is None
    assert "not included in the Homebrew binary distribution" in capsys.readouterr().err


def test_frozen_semantic_install_hint_never_mentions_uv(monkeypatch):
    from jarvis import runtime
    from jarvis.embeddings import semantic_install_hint

    monkeypatch.setattr(runtime, "is_frozen", lambda: True)
    frozen_hint = semantic_install_hint()
    assert "Homebrew binary distribution" in frozen_hint
    assert "uv" not in frozen_hint
    monkeypatch.setattr(runtime, "is_frozen", lambda: False)
    assert "uv" in semantic_install_hint()
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
uv run pytest \
  tests/test_index_cli.py::test_version_flag_reports_distribution_version \
  tests/test_index_cli.py::test_semantic_offer_is_structurally_disabled_in_frozen_build \
  tests/test_index_cli.py::test_frozen_semantic_skip_names_homebrew_distribution -q
```

Expected: FAIL because `--version` is unknown and frozen behavior is unchanged.

- [ ] **Step 3: Implement**

Add imports:

```python
from importlib.metadata import PackageNotFoundError, version as distribution_version
from jarvis import runtime
```

Add near the CLI helpers:

```python
def _distribution_version() -> str:
    try:
        return distribution_version("jarvis-mcp")
    except PackageNotFoundError:
        return "unknown"
```

In `build_parser`, immediately after constructing `parser`:

```python
    parser.add_argument(
        "--version", action="version", version=f"jarvis {_distribution_version()}"
    )
```

At the top of `_prepare_semantic_stage`, before importing `jarvis.semantic`, add:

```python
    if runtime.is_frozen():
        print(
            "semantic indexing skipped — semantic search is not included "
            "in the Homebrew binary distribution",
            file=sys.stderr,
        )
        return None
```

Keep the existing non-frozen `ImportError` hint unchanged. Extend the semantic-offer gate:

```python
    if (
        getattr(args, "offer_semantic", False)
        and not runtime.is_frozen()
        and _semantic_extra_missing()
        and _at_interactive_tty()
    ):
```

In `embeddings.py`, replace the fixed `_INSTALL_HINT` usage with:

```python
def semantic_install_hint() -> str:
    if runtime.is_frozen():
        return (
            "semantic search is not included in the Homebrew binary "
            "distribution"
        )
    return _INSTALL_HINT
```

Import `from jarvis import runtime`, keep the existing source-install `_INSTALL_HINT` string, and change `SemanticStore._connect` to raise with `semantic_install_hint()`. In `semantic.py`, import `semantic_install_hint` instead of `_INSTALL_HINT` and use it in the same raise. This ensures the MCP `semanticSearch` error envelope never tells a frozen user to run uv.

- [ ] **Step 4: Run focused and broad tests**

```bash
uv run pytest tests/test_runtime.py \
  tests/test_index_cli.py::test_version_flag_reports_distribution_version \
  tests/test_index_cli.py::test_semantic_offer_is_structurally_disabled_in_frozen_build \
  tests/test_index_cli.py::test_frozen_semantic_skip_names_homebrew_distribution \
  tests/test_index_cli.py::test_frozen_semantic_install_hint_never_mentions_uv -q
uv run pytest -m "not integration" -q
```

Expected: both commands PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/index_cli.py src/jarvis/embeddings.py src/jarvis/semantic.py \
  tests/test_index_cli.py
git commit -m "feat(cli): support frozen distribution behavior"
```

---

### Task 3: Single native launcher and packaging dependency

**Files:**
- Create: `packaging/launcher.py`
- Create: `tests/test_packaging_launcher.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: `runtime.initialize()`
- Produces: `select_entrypoint(program: str) -> str`
- Produces: `run(cli_main=None, server_main=None, runtime_initialize=runtime.initialize) -> int | None`
- Task 5 uses `packaging/launcher.py` as the sole PyInstaller entry script.

- [ ] **Step 1: Write failing tests**

Create `tests/test_packaging_launcher.py`:

```python
"""Tests for the PyInstaller argv[0] launcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "launcher.py"
spec = importlib.util.spec_from_file_location("jarvis_packaging_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_selects_cli_for_jarvis_names():
    assert launcher.select_entrypoint("/opt/homebrew/bin/jarvis") == "cli"
    assert launcher.select_entrypoint("jarvis") == "cli"


def test_selects_server_for_jarvis_server_name():
    assert launcher.select_entrypoint("/opt/homebrew/bin/jarvis-server") == "server"


def test_unknown_name_fails_loudly():
    try:
        launcher.select_entrypoint("/opt/homebrew/bin/unknown")
    except SystemExit as exc:
        assert "jarvis-server" in str(exc)
    else:
        raise AssertionError("unknown argv[0] must exit")


def test_run_dispatches_server(monkeypatch, tmp_path):
    fake = tmp_path / "native-bin"
    fake.mkdir()
    monkeypatch.setattr(launcher.sys, "argv", ["/somewhere/jarvis-server"])
    calls: list[str] = []
    result = launcher.run(
        cli_main=lambda: calls.append("cli") or 0,
        server_main=lambda: calls.append("server") or None,
        runtime_initialize=lambda base=None: fake,
    )
    assert result is None
    assert calls == ["server"]
```

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/test_packaging_launcher.py -q`  
Expected: FAIL because `packaging/launcher.py` does not exist.

- [ ] **Step 3: Implement the launcher**

Create `packaging/launcher.py`:

```python
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
```

Add the dependency group:

```toml
native = [
    "pyinstaller==6.14.1",
]
```

- [ ] **Step 4: Sync and run tests**

```bash
uv sync --group native
uv run pytest tests/test_packaging_launcher.py tests/test_runtime.py -q
uv lock --check
```

Expected: all checks PASS.

- [ ] **Step 5: Commit**

```bash
git add packaging/launcher.py tests/test_packaging_launcher.py pyproject.toml uv.lock
git commit -m "build(native): add single pyinstaller launcher"
```

---

### Task 4: Pinned native binary fetcher

**Files:**
- Create: `scripts/fetch_native_binaries.py`
- Create: `tests/test_fetch_native_binaries.py`

**Interfaces:**
- Produces: `split_platform(platform: str) -> tuple[str, str]`
- Produces: `asset_urls(platform: str) -> list[tuple[str, str, tuple[str, ...]]]`
- Produces: `fetch(platform: str, output: Path, only: str | None = None) -> None`
- Produces CLI: `uv run python scripts/fetch_native_binaries.py --platform PLATFORM --output DIR`
- Task 5 expects the output to contain executable `scip`, `zoekt-git-index`, and `zoekt-webserver`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_fetch_native_binaries.py`:

```python
"""Offline tests for jarvis's pinned native-binary fetcher."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_native_binaries.py"
spec = importlib.util.spec_from_file_location("fetch_native_binaries", SCRIPT)
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


def test_splits_supported_platforms():
    assert fetcher.split_platform("darwin_arm64") == ("darwin", "arm64")
    assert fetcher.split_platform("linux_amd64") == ("linux", "amd64")


def test_rejects_unsupported_platform():
    try:
        fetcher.split_platform("windows_amd64")
    except ValueError as exc:
        assert "windows_amd64" in str(exc)
    else:
        raise AssertionError("unsupported platform must fail")


def test_asset_urls_follow_public_pins():
    urls = fetcher.asset_urls("darwin_arm64")
    assert len(urls) == 2
    assert urls[0][0].startswith(
        "https://github.com/jarvis-intelligence/jarvis-index/releases/download/scip-"
    )
    assert urls[0][2] == ("scip",)
    assert urls[1][2] == ("zoekt-git-index", "zoekt-webserver")


def test_fetch_verifies_checksum_and_extracts_scip(tmp_path, monkeypatch):
    output = tmp_path / "native"
    output.mkdir()
    archives: dict[str, bytes] = {}

    def fake_download(url: str, destination: Path) -> None:
        if url in archives:
            destination.write_bytes(archives[url])
            return
        raw = io.BytesIO()
        data = b"#!/bin/sh\ntrue\n"
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            info = tarfile.TarInfo("scip")
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
        blob = raw.getvalue()
        archives[url] = blob
        archives[url + ".sha256"] = (
            hashlib.sha256(blob).hexdigest() + "  archive.tar.gz\n"
        ).encode()
        destination.write_bytes(blob)

    monkeypatch.setattr(fetcher, "download_to", fake_download)
    fetcher.fetch("darwin_arm64", output, only="scip")
    assert (output / "scip").read_bytes().startswith(b"#!/bin/sh")
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_fetch_native_binaries.py -q`  
Expected: FAIL because the script does not exist.

- [ ] **Step 3: Implement the fetcher**

Create `scripts/fetch_native_binaries.py` with these complete functions:

```python
"""Fetch pinned public SCIP and Zoekt binaries for native packaging."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_REPO = "https://github.com/jarvis-intelligence/jarvis-index/releases/download"
PLATFORM_RE = re.compile(r"^(darwin|linux)_(arm64|amd64)$")


def split_platform(platform: str) -> tuple[str, str]:
    match = PLATFORM_RE.fullmatch(platform)
    if match is None:
        raise ValueError(f"unsupported native platform {platform!r}")
    return match.group(1), match.group(2)


def _pin(tool: str) -> str:
    return (ROOT / f"{tool.upper()}_COMMIT").read_text().strip()


def asset_urls(platform: str) -> list[tuple[str, str, tuple[str, ...]]]:
    os_name, arch = split_platform(platform)
    scip_tag = f"scip-{_pin('scip')}"
    zoekt_tag = f"zoekt-{_pin('zoekt')}"
    return [
        (
            f"{RELEASE_REPO}/{scip_tag}/scip-{os_name}-{arch}.tar.gz",
            f"{RELEASE_REPO}/{scip_tag}/scip-{os_name}-{arch}.tar.gz.sha256",
            ("scip",),
        ),
        (
            f"{RELEASE_REPO}/{zoekt_tag}/zoekt-{os_name}-{arch}.tar.gz",
            f"{RELEASE_REPO}/{zoekt_tag}/zoekt-{os_name}-{arch}.tar.gz.sha256",
            ("zoekt-git-index", "zoekt-webserver"),
        ),
    ]


def download_to(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response:
        destination.write_bytes(response.read())


def fetch(platform: str, output: Path, only: str | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if only is not None and only not in {"scip", "zoekt"}:
        raise ValueError("--only must be 'scip' or 'zoekt'")
    for archive_url, checksum_url, members in asset_urls(platform):
        tool = "scip" if members == ("scip",) else "zoekt"
        if only is not None and tool != only:
            continue
        with tempfile.TemporaryDirectory(prefix="jarvis-native-") as temporary:
            work = Path(temporary)
            archive = work / "archive.tar.gz"
            checksum = work / "archive.sha256"
            download_to(archive_url, archive)
            download_to(checksum_url, checksum)
            expected = checksum.read_text().split()[0]
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if expected != actual:
                raise RuntimeError(
                    f"checksum mismatch for {archive_url}: "
                    f"expected {expected}, got {actual}"
                )
            with tarfile.open(archive, "r:gz") as tar:
                selected = []
                for name in members:
                    member = tar.getmember(name)
                    if not member.isfile():
                        raise RuntimeError(f"release member is not a file: {name}")
                    selected.append(member)
                tar.extractall(work, members=selected, filter="data")
            for name in members:
                destination = output / name
                shutil.move(work / name, destination)
                destination.chmod(0o755)
```

Add `main(argv: list[str] | None = None) -> int` with required `--platform`, required `--output`, optional `--only`, and this exact failure boundary:

```python
    except (OSError, RuntimeError, tarfile.TarError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
```

- [ ] **Step 4: Verify the tests pass**

Run: `uv run pytest tests/test_fetch_native_binaries.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/fetch_native_binaries.py tests/test_fetch_native_binaries.py
git commit -m "build(native): fetch pinned scip and zoekt binaries"
```

---

### Task 5: PyInstaller runner, spec, and archive stager

**Files:**
- Create: `packaging/jarvis.spec`
- Create: `scripts/run_pyinstaller.py`
- Create: `scripts/stage_native_release.py`
- Create: `tests/test_stage_native_release.py`

**Interfaces:**
- Consumes `JARVIS_NATIVE_BIN_DIR`, `JARVIS_APP_OUTPUT_DIR`, and `JARVIS_PYINSTALLER_WORK_DIR`.
- Produces: `run_pyinstaller.main() -> int`.
- Produces: `stage_native_release(app_dir: Path, native_bin_dir: Path, output_dir: Path, version: str, platform: str) -> tuple[Path, Path]`.
- Produces `jarvis_{version}_{platform}.tar.gz` and `.sha256`.

- [ ] **Step 1: Write failing stager tests**

Create `tests/test_stage_native_release.py`:

```python
"""Tests for shaping and archiving PyInstaller output."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage_native_release.py"
spec = importlib.util.spec_from_file_location("stage_native_release", SCRIPT)
stager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stager)


def make_app(parent: Path) -> Path:
    app_dir = parent / "app"
    app = app_dir / "jarvis"
    (app / "_internal").mkdir(parents=True)
    launcher = app / "jarvis"
    launcher.write_text("#!/bin/sh\ntrue\n")
    launcher.chmod(0o755)
    (app / "_internal" / "base_library.zip").write_bytes(b"runtime")
    return app_dir


def make_native(parent: Path) -> Path:
    native = parent / "native"
    native.mkdir()
    for name in ("scip", "zoekt-git-index", "zoekt-webserver"):
        path = native / name
        path.write_text("#!/bin/sh\ntrue\n")
        path.chmod(0o755)
    return native


def test_stage_creates_public_names_and_checksummed_archive(tmp_path):
    app_dir = make_app(tmp_path)
    native = make_native(tmp_path)
    output = tmp_path / "release"
    archive, checksum = stager.stage_native_release(
        app_dir, native, output, "0.11.0", "darwin_arm64"
    )
    root = output / "jarvis"
    assert archive.name == "jarvis_0.11.0_darwin_arm64.tar.gz"
    assert checksum.name == archive.name + ".sha256"
    assert (root / "libexec" / "jarvis").is_file()
    assert os.path.islink(root / "bin" / "jarvis")
    assert os.path.islink(root / "bin" / "jarvis-server")
    assert (root / "libexec" / "_internal" / "native-bin" / "scip").is_file()
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_text().split()[0] == expected
    with tarfile.open(archive, "r:gz") as tar:
        assert "jarvis/bin/jarvis-server" in tar.getnames()


def test_stage_rejects_missing_launcher(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    native = make_native(tmp_path)
    try:
        stager.stage_native_release(
            app_dir, native, tmp_path / "out", "0.11.0", "linux_amd64"
        )
    except RuntimeError as exc:
        assert "app/jarvis/jarvis" in str(exc)
    else:
        raise AssertionError("missing launcher must fail")
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_stage_native_release.py -q`  
Expected: FAIL because the stager does not exist.

- [ ] **Step 3: Implement the spec and runner**

Create `packaging/jarvis.spec`:

```python
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
launcher = Path(__file__).with_name("launcher.py")
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
```

Create `scripts/run_pyinstaller.py`:

```python
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
```

- [ ] **Step 4: Implement the stager**

Create `scripts/stage_native_release.py` with this executable helper:

```python
def is_executable(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o111 != 0
```

The public stager function must validate the platform and required executable files, then create exactly:

```text
output_dir/jarvis/bin/jarvis        -> ../libexec/jarvis
output_dir/jarvis/bin/jarvis-server -> ../libexec/jarvis
output_dir/jarvis/libexec/jarvis
output_dir/jarvis/libexec/_internal/
output_dir/jarvis/libexec/_internal/native-bin/
```

Copy the PyInstaller launcher and `_internal` tree, copy all three native binaries, tar with `dereference=False`, write `sha256  filename\n`, and return `(archive_path, checksum_path)`. Add a CLI accepting `--app-dir`, `--native-bin-dir`, `--output-dir`, `--version`, and `--platform`; map `OSError`, `RuntimeError`, and `ValueError` to stderr plus return code 1.

- [ ] **Step 5: Verify the tests pass**

Run: `uv run pytest tests/test_stage_native_release.py -q`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packaging/jarvis.spec scripts/run_pyinstaller.py scripts/stage_native_release.py tests/test_stage_native_release.py
git commit -m "build(native): stage pyinstaller archive"
```

---

### Task 6: Native package checker

**Files:**
- Create: `scripts/check_native_package.py`
- Create: `tests/test_check_native_package.py`

**Interfaces:**
- Produces: `validate(root: Path, platform: str, file_types: dict[Path, str] | None = None) -> list[str]`
- Produces CLI: `uv run python scripts/check_native_package.py ARCHIVE --platform PLATFORM`
- Task 9 fails the release when this returns any problem.

- [ ] **Step 1: Write failing checker tests**

Create `tests/test_check_native_package.py`:

```python
"""Tests for standalone archive validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
    for name in ("scip", "zoekt-git-index", "zoekt-webserver"):
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


def test_valid_tree_passes_with_injected_file_type(tmp_path):
    root = valid_tree(tmp_path)
    launcher = root / "libexec" / "jarvis"
    assert checker.validate(root, "darwin_arm64", {launcher: "Mach-O arm64"}) == []


def test_rejects_readable_jarvis_source(tmp_path):
    root = valid_tree(tmp_path)
    source = root / "libexec" / "_internal" / "jarvis" / "query.py"
    source.write_text("def f(): pass")
    problems = checker.validate(root, "darwin_arm64", {})
    assert any("readable jarvis source" in problem for problem in problems)


def test_rejects_semantic_dependency(tmp_path):
    root = valid_tree(tmp_path)
    semantic = root / "libexec" / "_internal" / "lancedb"
    semantic.mkdir()
    (semantic / "__init__.py").write_text("")
    problems = checker.validate(root, "darwin_arm64", {})
    assert any("semantic dependency" in problem for problem in problems)


def test_rejects_missing_native_binary_and_wrong_architecture(tmp_path):
    root = valid_tree(tmp_path)
    (root / "libexec" / "_internal" / "native-bin" / "scip").unlink()
    launcher = root / "libexec" / "jarvis"
    problems = checker.validate(root, "darwin_arm64", {launcher: "Mach-O x86_64"})
    assert any("missing executable native binary: scip" in p for p in problems)
    assert any("wrong architecture" in problem for problem in problems)
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_check_native_package.py -q`  
Expected: FAIL because the checker does not exist.

- [ ] **Step 3: Implement the checker**

Create `scripts/check_native_package.py` with these constants:

```python
GRAMMARS = (
    "python", "javascript", "typescript", "java", "kotlin", "swift", "go",
    "ruby", "rust", "c", "cpp", "c_sharp", "php", "scala", "bash", "sql",
)
REQUIRED_NATIVE = ("scip", "zoekt-git-index", "zoekt-webserver")
REQUIRED_MODULES = ("query", "syntax", "index_cli", "server", "dashboard")
FORBIDDEN_SEMANTIC = ("lancedb", "sentence_transformers", "torch")
SOURCE_EXCEPTIONS = {"__init__.py", "scip_pb2.py"}
ARCH_PATTERNS = {
    "darwin_arm64": ("Mach-O", "arm64"),
    "darwin_amd64": ("Mach-O", "x86_64"),
    "linux_arm64": ("ELF", "aarch64"),
    "linux_amd64": ("ELF", "x86_64"),
}
```

Implement:

```python
def is_executable(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o111 != 0


def file_type(path: Path) -> str:
    result = subprocess.run(
        ["file", str(path)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()
```

`validate(root, platform, file_types=None)` must append one specific message for each violation:

- unsupported platform;
- missing executable `root/libexec/jarvis`;
- each missing executable native binary;
- each missing dashboard asset `index.html`, `app.js`, `style.css`;
- each missing compiled module `jarvis/{query,syntax,index_cli,server,dashboard}.cpython-*.so`;
- each missing `tree_sitter_{grammar}*.so`;
- each readable `jarvis/*.py` except `__init__.py` and `scip_pb2.py`;
- each bundled semantic dependency;
- bundled `uv`, `uvx`, `pip`, or `pip3` executable;
- launcher architecture not matching the selected platform.

Use exactly this architecture check:

```python
    types = file_types if file_types is not None else {launcher: file_type(launcher)}
    expected_format, expected_arch = ARCH_PATTERNS[platform]
    description = types.get(launcher, "")
    if expected_format not in description or expected_arch not in description:
        problems.append(
            f"wrong architecture for {platform}: {description!r}"
        )
```

The CLI accepts one tar.gz and `--platform`, safely extracts it with `filter="data"` into a temporary directory, validates the top-level `jarvis/` directory, prints every problem to stderr, and returns 1 for any problem.

- [ ] **Step 4: Verify the tests pass**

Run: `uv run pytest tests/test_check_native_package.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_native_package.py tests/test_check_native_package.py
git commit -m "test(native): validate standalone package contents"
```

---

### Task 7: Homebrew formula generator

**Files:**
- Create: `scripts/generate_homebrew_formula.py`
- Create: `tests/test_generate_homebrew_formula.py`

**Interfaces:**
- Produces: `PLATFORMS: tuple[str, ...]`
- Produces: `render(version: str, release_tag: str, checksums: dict[str, str]) -> str`
- Produces CLI: `uv run python scripts/generate_homebrew_formula.py --version V --release-tag TAG --checksum-dir DIR`

- [ ] **Step 1: Write failing tests**

Create `tests/test_generate_homebrew_formula.py`:

```python
"""Tests for the jarvis Homebrew formula generator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_homebrew_formula.py"
spec = importlib.util.spec_from_file_location("generate_homebrew_formula", SCRIPT)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def test_render_contains_every_platform_url_and_checksum():
    checksums = {
        platform: "a" * 63 + str(index)
        for index, platform in enumerate(generator.PLATFORMS)
    }
    formula = generator.render("0.11.0", "v0.11.0", checksums)
    assert 'version "0.11.0"' in formula
    assert 'depends_on "universal-ctags"' in formula
    for platform, digest in checksums.items():
        assert f"jarvis_0.11.0_{platform}.tar.gz" in formula
        assert digest in formula


def test_render_installs_both_names_and_ctags_shim():
    checksums = {
        platform: "a" * 63 + str(index)
        for index, platform in enumerate(generator.PLATFORMS)
    }
    formula = generator.render("0.11.0", "v0.11.0", checksums)
    assert 'bin.install_symlink libexec/"jarvis" => "jarvis"' in formula
    assert 'bin.install_symlink libexec/"jarvis" => "jarvis-server"' in formula
    assert "universal-ctags" in formula


def test_render_rejects_missing_platform_and_bad_version():
    missing = {platform: "a" * 64 for platform in generator.PLATFORMS}
    missing.pop("linux_arm64")
    try:
        generator.render("0.11.0", "v0.11.0", missing)
    except ValueError as exc:
        assert "linux_arm64" in str(exc)
    else:
        raise AssertionError("missing checksum must fail")
    try:
        generator.render("latest", "vlatest", dict.fromkeys(generator.PLATFORMS, "a" * 64))
    except ValueError as exc:
        assert "latest" in str(exc)
    else:
        raise AssertionError("non-semantic version must fail")
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_generate_homebrew_formula.py -q`  
Expected: FAIL because the generator does not exist.

- [ ] **Step 3: Implement the generator**

Create `scripts/generate_homebrew_formula.py` around this exact Ruby template:

```ruby
# Generated by jarvis's publish-native workflow; do not edit by hand.
class Jarvis < Formula
  desc "Local-first code intelligence MCP server"
  homepage "https://github.com/jarvis-intelligence/jarvis-index"
  url "{{ initial_url }}"
  sha256 "{{ initial_sha }}"
  version "{{ version }}"

  depends_on "universal-ctags"

  on_macos do
    on_arm do
      url "{{ darwin_arm64_url }}"
      sha256 "{{ darwin_arm64_sha }}"
    end
    on_intel do
      url "{{ darwin_amd64_url }}"
      sha256 "{{ darwin_amd64_sha }}"
    end
  end

  on_linux do
    on_arm do
      url "{{ linux_arm64_url }}"
      sha256 "{{ linux_arm64_sha }}"
    end
    on_intel do
      url "{{ linux_amd64_url }}"
      sha256 "{{ linux_amd64_sha }}"
    end
  end

  def install
    libexec.install Dir["jarvis/libexec/*"]
    bin.install_symlink libexec/"jarvis" => "jarvis"
    bin.install_symlink libexec/"jarvis" => "jarvis-server"

    ctags = Formula["universal-ctags"].opt_bin/"ctags"
    (libexec/"_internal/native-bin/universal-ctags").write <<~SH
      #!/bin/sh
      exec "#{ctags}" "$@"
    SH
    chmod 0o755, libexec/"_internal/native-bin/universal-ctags"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/jarvis --version")
  end
end
```

Implementation rules:

- `PLATFORMS = ("darwin_arm64", "darwin_amd64", "linux_arm64", "linux_amd64")`.
- Base URL: `https://github.com/jarvis-intelligence/homebrew-jarvis/releases/download/{release_tag}/jarvis_{version}_{platform}.tar.gz`.
- Require semantic `X.Y.Z`, non-empty release tag, all four checksums, and 64-character lowercase hexadecimal digests.
- Read checksums from `jarvis_{version}_{platform}.tar.gz.sha256`.
- Write rendered Ruby to stdout; errors go to stderr with return code 1.

Because the Python renderer uses an f-string, escape every Ruby interpolation
brace (`{{ctags}}`, `{{bin}}` in the source template); the rendered Ruby must
contain the normal `#{ctags}` and `#{bin}` forms shown above.

- [ ] **Step 4: Verify tests and generated Ruby syntax**

```bash
uv run pytest tests/test_generate_homebrew_formula.py -q
```

Expected: PASS. If a local Homebrew `ruby` is available, additionally render a temporary formula and run `ruby -c TEMP.rb`; syntax checking is optional because CI validates the real formula with Homebrew.

- [ ] **Step 5: Commit**

```bash
git add scripts/generate_homebrew_formula.py tests/test_generate_homebrew_formula.py
git commit -m "build(homebrew): generate jarvis formula"
```

---

### Task 8: Extracted-artifact smoke harness

**Files:**
- Create: `scripts/native_smoke.py`
- Create: `tests/test_native_smoke.py`

**Interfaces:**
- Produces: `jsonrpc_line(payload: dict[str, object]) -> str`
- Produces: `semantic_unavailable(response: dict[str, Any]) -> bool`
- Produces: `McpStdioClient(command: str, env: dict[str, str])`
- Produces CLI: `uv run python scripts/native_smoke.py --root ROOT --repo REPO --data-dir DIR`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_native_smoke.py`:

```python
"""Unit tests for native smoke-test helper seams."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "native_smoke.py"
spec = importlib.util.spec_from_file_location("native_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_jsonrpc_line_is_single_line_json():
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    encoded = smoke.jsonrpc_line(payload)
    assert encoded.endswith("\n")
    assert json.loads(encoded) == payload


def test_semantic_failure_detects_tool_error():
    response = {
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"error": "semantic unavailable"})}
            ],
            "isError": True,
        }
    }
    assert smoke.semantic_unavailable(response) is True


def test_semantic_failure_detects_error_envelope():
    response = {
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"error": "semantic unavailable"})}
            ]
        }
    }
    assert smoke.semantic_unavailable(response) is True


def test_successful_response_is_not_semantic_failure():
    response = {
        "result": {
            "content": [{"type": "text", "text": json.dumps({"results": []})}]
        }
    }
    assert smoke.semantic_unavailable(response) is False
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_native_smoke.py -q`  
Expected: FAIL because the harness does not exist.

- [ ] **Step 3: Implement the harness**

Create `scripts/native_smoke.py` with these complete helpers:

```python
def jsonrpc_line(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":")) + "\n"


def text_body(result: dict[str, Any]) -> str:
    return "".join(item.get("text", "") for item in result.get("content", []))


def semantic_unavailable(response: dict[str, Any]) -> bool:
    result = response.get("result", {})
    return (
        response.get("error") is not None
        or result.get("isError") is True
        or '"error"' in text_body(result)
    )
```

Implement `McpStdioClient` with:

```python
class McpStdioClient:
    def __init__(self, command: str, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            [command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
```

It must expose `send`, `receive`, `request(identifier, method, params=None)`, `notify(method)`, and `close()`. `receive()` raises `RuntimeError` with the server’s stderr tail when stdout closes, and rejects a JSON-RPC `error` member.

Implement a clean environment helper:

```python
def native_environment(root: Path, data_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["JARVIS_DATA_DIR"] = str(data_dir)
    paths = [str(root / "bin")]
    if env.get("NATIVE_SMOKE_EXTRA_PATH"):
        paths.append(env["NATIVE_SMOKE_EXTRA_PATH"])
    paths.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    env["PATH"] = os.pathsep.join(paths)
    return env
```

Main must:

1. Run `jarvis --version`.
2. Run `jarvis index REPO --slug native-smoke`.
3. Start `jarvis watch REPO --slug native-smoke --debounce 5`, wait one second, assert it has not exited, terminate it, and wait.
4. Start `jarvis-server` with the clean environment.
5. Complete MCP `initialize`, `notifications/initialized`, and `tools/list`; assert exactly 10 tools.
6. Call `findReferences(repo="native-smoke", symbol="greet")`; assert `greeter.py` appears.
7. Call `searchCode(query="hello", repo="native-smoke")`; assert `greeter.py` appears.
8. Call `semanticSearch(repo="native-smoke", query="greeting")`; assert `semantic_unavailable`.
9. Assert the semantic response text contains `Homebrew binary distribution`.
10. Always close the MCP child and print `native smoke: PASS`.

- [ ] **Step 4: Verify helper tests pass**

Run: `uv run pytest tests/test_native_smoke.py -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/native_smoke.py tests/test_native_smoke.py
git commit -m "test(native): smoke test standalone artifact"
```

---

### Task 9: Native CI build helper and release workflow

**Files:**
- Create: `scripts/ci_native_build.sh`
- Create: `.github/workflows/publish-native.yml`

**Interfaces:**
- Consumes Tasks 3–8.
- Produces four `.tar.gz`/`.sha256` artifacts under `.native-release/release/`.
- Produces a generated Homebrew formula.
- Produces a formula commit in `jarvis-intelligence/homebrew-jarvis`.

- [ ] **Step 1: Implement the CI helper**

Create executable `scripts/ci_native_build.sh`:

```bash
#!/usr/bin/env bash
# Build one standalone jarvis archive. Linux builds in manylinux containers.
set -euo pipefail

INSIDE=0
if [ "${1:-}" = "--inside" ]; then
    INSIDE=1
    shift
fi
PLATFORM="${1:?usage: ci_native_build.sh [--inside] PLATFORM}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

inside_build() {
    local platform="$1"
    local python="3.12"
    if [[ "$platform" == linux_* ]]; then
        python="/opt/python/cp312-cp312/bin/python3"
    fi

    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    if ! command -v file >/dev/null 2>&1; then
        yum install -y file
    fi

    export UV_PYTHON="$python"
    export UV_PROJECT_ENVIRONMENT="$PWD/.native-release/project"
    rm -rf .native-release
    mkdir -p .native-release/wheels .native-release/native \
             .native-release/app .native-release/release

    uv sync --group native
    uv run pytest -m "not integration" -rs
    JARVIS_COMPILE=1 uv build --python "$UV_PYTHON" --wheel \
        --out-dir .native-release/wheels

    uv venv --python "$UV_PYTHON" .native-release/package
    uv pip install --python .native-release/package/bin/python \
        .native-release/wheels/jarvis_mcp-*.whl 'watchdog>=4' pyinstaller==6.14.1

    uv run python scripts/fetch_native_binaries.py \
        --platform "$platform" --output .native-release/native
    JARVIS_NATIVE_BIN_DIR="$PWD/.native-release/native" \
    JARVIS_APP_OUTPUT_DIR="$PWD/.native-release/app" \
    JARVIS_PYINSTALLER_WORK_DIR="$PWD/.native-release/pyinstaller-work" \
        .native-release/package/bin/python scripts/run_pyinstaller.py

    local version
    version="$(uv version --short)"
    uv run python scripts/stage_native_release.py \
        --app-dir .native-release/app --native-bin-dir .native-release/native \
        --output-dir .native-release/release --version "$version" \
        --platform "$platform"
    uv run python scripts/check_native_package.py \
        ".native-release/release/jarvis_${version}_${platform}.tar.gz" \
        --platform "$platform"
}

if [ "$INSIDE" -eq 1 ]; then
    inside_build "$PLATFORM"
    exit 0
fi

case "$PLATFORM" in
    darwin_arm64 | darwin_amd64)
        inside_build "$PLATFORM"
        ;;
    linux_amd64)
        docker run --rm -v "$PWD:$PWD" -w "$PWD" \
            quay.io/pypa/manylinux_2_28_x86_64 \
            bash scripts/ci_native_build.sh --inside "$PLATFORM"
        ;;
    linux_arm64)
        docker run --rm -v "$PWD:$PWD" -w "$PWD" \
            quay.io/pypa/manylinux_2_28_aarch64 \
            bash scripts/ci_native_build.sh --inside "$PLATFORM"
        ;;
    *)
        echo "error: unsupported platform: $PLATFORM" >&2
        exit 2
        ;;
esac
```

- [ ] **Step 2: Add the release workflow**

Create `.github/workflows/publish-native.yml`:

```yaml
name: publish-native

on:
  release:
    types: [published]

permissions:
  contents: read

jobs:
  build:
    strategy:
      fail-fast: true
      matrix:
        platform: [darwin_arm64, darwin_amd64, linux_amd64, linux_arm64]
        include:
          - platform: darwin_arm64
            runner: macos-latest
          - platform: darwin_amd64
            runner: macos-15-intel
          - platform: linux_amd64
            runner: ubuntu-latest
          - platform: linux_arm64
            runner: ubuntu-24.04-arm
    runs-on: ${{ matrix.runner }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - name: Build one checked native archive
        run: bash scripts/ci_native_build.sh "$PLATFORM"
        env:
          PLATFORM: ${{ matrix.platform }}
      - name: Smoke test extracted archive
        run: |
          set -eu
          version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)"
          mkdir -p "$RUNNER_TEMP/extract" "$RUNNER_TEMP/data"
          rm -rf "$RUNNER_TEMP/mini_py_repo"
          cp -R tests/fixtures/mini_py_repo "$RUNNER_TEMP/mini_py_repo"
          git -C "$RUNNER_TEMP/mini_py_repo" init -q
          git -C "$RUNNER_TEMP/mini_py_repo" config user.email smoke@example.invalid
          git -C "$RUNNER_TEMP/mini_py_repo" config user.name "Native Smoke"
          git -C "$RUNNER_TEMP/mini_py_repo" add greeter.py
          git -C "$RUNNER_TEMP/mini_py_repo" commit -qm "fixture"
          tar -xzf ".native-release/release/jarvis_${version}_${PLATFORM}.tar.gz" \
            -C "$RUNNER_TEMP/extract"
          npm install --silent -g @sourcegraph/scip-python
          export NATIVE_SMOKE_EXTRA_PATH="$(dirname "$(command -v scip-python)")"
          .native-release/project/bin/python scripts/native_smoke.py \
            --root "$RUNNER_TEMP/extract/jarvis" \
            --repo "$RUNNER_TEMP/mini_py_repo" \
            --data-dir "$RUNNER_TEMP/data"
        env:
          PLATFORM: ${{ matrix.platform }}
      - uses: actions/upload-artifact@v4
        with:
          name: native-${{ matrix.platform }}
          path: .native-release/release/*
          if-no-files-found: error

  stage-release:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - uses: actions/download-artifact@v4
        with:
          pattern: native-*
          merge-multiple: true
          path: release
      - name: Verify tag, upload assets, and render formula
        env:
          GH_TOKEN: ${{ secrets.JARVIS_TAP_TOKEN }}
          RELEASE_TAG: ${{ github.event.release.tag_name }}
        run: |
          set -eu
          version="$(uv version --short)"
          tagged="${RELEASE_TAG#v}"
          if [ "$version" != "$tagged" ]; then
            echo "release tag $RELEASE_TAG does not match pyproject version $version" >&2
            exit 1
          fi
          for platform in darwin_arm64 darwin_amd64 linux_arm64 linux_amd64; do
            test -f "release/jarvis_${version}_${platform}.tar.gz"
            test -f "release/jarvis_${version}_${platform}.tar.gz.sha256"
          done
          if ! gh release view "v${version}" \
              --repo jarvis-intelligence/homebrew-jarvis >/dev/null 2>&1; then
            gh release create "v${version}" \
              --repo jarvis-intelligence/homebrew-jarvis \
              --title "jarvis ${version}" \
              --notes "Standalone jarvis ${version}."
          fi
          gh release upload "v${version}" release/* --clobber \
            --repo jarvis-intelligence/homebrew-jarvis
          uv run python scripts/generate_homebrew_formula.py \
            --version "$version" --release-tag "v${version}" \
            --checksum-dir release > jarvis.rb
      - uses: actions/upload-artifact@v4
        with:
          name: homebrew-formula
          path: jarvis.rb
          if-no-files-found: error

  validate-homebrew:
    needs: stage-release
    strategy:
      fail-fast: true
      matrix:
        os: [macos-latest, macos-15-intel, ubuntu-latest, ubuntu-24.04-arm]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: homebrew-formula
      - uses: Homebrew/actions/setup-homebrew@v4
      - name: Install and test formula
        run: |
          set -eu
          brew install --formula ./jarvis.rb
          brew test --formula ./jarvis.rb
          jarvis --version
          test "$(basename "$(command -v jarvis)")" = jarvis
          test "$(basename "$(command -v jarvis-server)")" = jarvis-server
      - name: Audit formula without blocking on custom-tap policy warnings
        run: brew audit --formula ./jarvis.rb || true

  publish-formula:
    needs: validate-homebrew
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: homebrew-formula
          path: formula
      - name: Download and publish generated formula
        env:
          GH_TOKEN: ${{ secrets.JARVIS_TAP_TOKEN }}
        run: |
          set -eu
          version="${GITHUB_REF_NAME#v}"
          rm -rf tap
          gh repo clone jarvis-intelligence/homebrew-jarvis tap -- --depth=1
          cp formula/jarvis.rb tap/Formula/jarvis.rb
          cd tap
          git config user.name "jarvis release bot"
          git config user.email "noreply@jarvis-intelligence.dev"
          git add Formula/jarvis.rb
          git commit -m "chore: publish jarvis ${version}"
          git push
```

- [ ] **Step 3: Validate scripts and workflow syntax**

```bash
bash -n scripts/ci_native_build.sh
ruby -ryaml -e 'YAML.safe_load(File.read(".github/workflows/publish-native.yml")); puts "workflow parses"'
git diff --check
```

Expected: all commands print no errors.

- [ ] **Step 4: Run unit suite**

Run: `uv run pytest -m "not integration" -q`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/ci_native_build.sh .github/workflows/publish-native.yml
git commit -m "ci(native): build and publish standalone archives"
```

---

### Task 10: Local macOS native build proof

**Files:**
- No tracked files should change.
- Generated files live only under ignored `.native-release/`.

**Interfaces:**
- Consumes Tasks 3–8.
- Produces a locally extracted, checker-approved, indexed darwin/arm64 archive.

- [ ] **Step 1: Build wheel and packaging environment**

```bash
rm -rf .native-release
mkdir -p .native-release/wheels .native-release/native \
         .native-release/app .native-release/release
uv sync --group native
JARVIS_COMPILE=1 uv build --python 3.12 --wheel --out-dir .native-release/wheels
uv venv --python 3.12 .native-release/package
uv pip install --python .native-release/package/bin/python \
  .native-release/wheels/jarvis_mcp-*.whl 'watchdog>=4' pyinstaller==6.14.1
```

- [ ] **Step 2: Fetch binaries and package**

```bash
uv run python scripts/fetch_native_binaries.py \
  --platform darwin_arm64 --output .native-release/native
JARVIS_NATIVE_BIN_DIR="$PWD/.native-release/native" \
JARVIS_APP_OUTPUT_DIR="$PWD/.native-release/app" \
JARVIS_PYINSTALLER_WORK_DIR="$PWD/.native-release/pyinstaller-work" \
  .native-release/package/bin/python scripts/run_pyinstaller.py
version="$(uv version --short)"
uv run python scripts/stage_native_release.py \
  --app-dir .native-release/app --native-bin-dir .native-release/native \
  --output-dir .native-release/release --version "$version" \
  --platform darwin_arm64
uv run python scripts/check_native_package.py \
  ".native-release/release/jarvis_${version}_darwin_arm64.tar.gz" \
  --platform darwin_arm64
```

- [ ] **Step 3: Extract and run local smoke**

```bash
mkdir -p .native-release/extract .native-release/data
rm -rf .native-release/mini_py_repo
cp -R tests/fixtures/mini_py_repo .native-release/mini_py_repo
git -C .native-release/mini_py_repo init -q
git -C .native-release/mini_py_repo config user.email smoke@example.invalid
git -C .native-release/mini_py_repo config user.name "Native Smoke"
git -C .native-release/mini_py_repo add greeter.py
git -C .native-release/mini_py_repo commit -qm "fixture"
tar -xzf .native-release/release/jarvis_*_darwin_arm64.tar.gz \
  -C .native-release/extract
command -v scip-python >/dev/null || npm install -g @sourcegraph/scip-python
uv run python scripts/native_smoke.py \
  --root "$PWD/.native-release/extract/jarvis" \
  --repo "$PWD/.native-release/mini_py_repo" \
  --data-dir "$PWD/.native-release/data"
```

Expected: `native smoke: PASS`.

- [ ] **Step 4: Confirm repository remains clean**

```bash
uv run pytest -m "not integration" -q
git status --short
```

Expected: tests PASS and no tracked source changes from generated artifacts. Do not commit `.native-release`; if packaging exposes a defect, open a separate focused fix with its own failing test rather than folding generated files into this task.

---

### Task 11: Remove legacy publication surfaces

**Files:**
- Delete: `.github/workflows/publish-pypi.yml`
- Delete: `.github/workflows/publish-mcp-registry.yml`
- Delete: `.github/workflows/sync-public-distribution.yml`
- Delete: `server.json`
- Modify: `scripts/check_versions.py`
- Test: `tests/test_check_versions.py`

**Interfaces:**
- Produces: `read_declared_versions(root: Path) -> dict[str, str]` with one entry, `pyproject.toml [project].version`.
- Produces: `check(root: Path) -> list[str]` that reports an unreadable or inconsistent local version.

- [ ] **Step 1: Write failing version tests**

Replace `tests/test_check_versions.py`:

```python
"""Tests for the single-source release version guard."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_versions.py"
spec = importlib.util.spec_from_file_location("check_versions", SCRIPT)
check_versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_versions)


def test_pyproject_is_the_single_declared_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "jarvis-mcp"\nversion = "0.11.0"\n'
    )
    assert check_versions.read_declared_versions(tmp_path) == {
        "pyproject.toml [project].version": "0.11.0"
    }
    assert check_versions.check(tmp_path) == []


def test_missing_or_invalid_pyproject_is_reported(tmp_path):
    problems = check_versions.check(tmp_path)
    assert len(problems) == 1
    assert "could not read release version" in problems[0]
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_check_versions.py -q`  
Expected: FAIL while the checker still requires `server.json`.

- [ ] **Step 3: Simplify the checker**

Replace the checker’s two public functions:

```python
def read_declared_versions(root: Path) -> dict[str, str]:
    """Map the sole local release-version declaration to its value."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    return {"pyproject.toml [project].version": pyproject["project"]["version"]}


def check(root: Path) -> list[str]:
    """Return problems; an empty list means the version is readable."""
    try:
        declared = read_declared_versions(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"could not read release version: {exc}"]
    if len(declared) != 1:
        return ["expected exactly one local release version declaration"]
    return []
```

Remove the now-unused `json` import. Update the module docstring to state that `pyproject.toml` is the only local release-version source.

- [ ] **Step 4: Delete legacy workflows and registry metadata**

```bash
git rm .github/workflows/publish-pypi.yml \
  .github/workflows/publish-mcp-registry.yml \
  .github/workflows/sync-public-distribution.yml server.json
```

Keep `.github/workflows/test.yml` unchanged; its existing `uv run python scripts/check_versions.py` step becomes the single-version guard.

- [ ] **Step 5: Verify cleanup**

```bash
uv run pytest tests/test_check_versions.py -q
uv run python scripts/check_versions.py
grep -RInE 'publish-pypi|publish-mcp-registry|sync-public-distribution' \
  .github scripts tests pyproject.toml || true
```

Expected: tests and checker PASS; no live workflow references remain.

- [ ] **Step 6: Commit**

```bash
git add scripts/check_versions.py tests/test_check_versions.py
git commit -m "chore(release): remove pypi and registry publication"
```

---

### Task 12: Reduce bootstrap script to language indexers

**Files:**
- Modify: `setup.sh`
- Test: `tests/test_setup_sh.py`

**Interfaces:**
- Produces valid `--only` choices: `scip-swift`, `scip-typescript`, `scip-python`, `scip-java`, and `bash-shim`.
- Removes `scip`, `zoekt`, `ctags`, and `jarvis-mcp` install orchestration.

- [ ] **Step 1: Write failing setup tests**

Delete the four tests under `# jarvis-mcp installer`. Add:

```python
def test_removed_common_installers_are_rejected():
    for removed in ("scip", "zoekt", "ctags", "jarvis-mcp"):
        result = run_func(f"parse_args --only {removed}")
        assert result.returncode != 0
        assert removed in result.stdout + result.stderr


def test_usage_lists_only_language_installers():
    text = SETUP_SH.read_text()
    start = text.index("Usage: setup.sh")
    end = text.index("Environment:", start)
    usage = text[start:end]
    for retained in (
        "scip-swift", "scip-typescript", "scip-python", "scip-java", "bash-shim"
    ):
        assert retained in usage
    for removed in ("scip,", "zoekt,", "ctags,", "jarvis-mcp"):
        assert removed not in usage


def test_common_installer_functions_are_removed():
    text = SETUP_SH.read_text()
    for function in (
        "install_scip()", "install_zoekt()", "install_ctags()",
        "install_jarvis_mcp()", "link_universal_ctags()",
    ):
        assert function not in text
```

Update or remove existing tests that assert old pin constants, old `--only` choices, or direct behavior of the removed functions. Retained language-indexer tests must continue unchanged.

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_setup_sh.py -q`  
Expected: FAIL because `setup.sh` still exposes the old installers.

- [ ] **Step 3: Reduce `setup.sh`**

Make these exact changes:

1. Remove `SCIP_COMMIT_PIN` and `ZOEKT_COMMIT_PIN`; Task 4 reads repo-root `SCIP_COMMIT` and `ZOEKT_COMMIT`.
2. Delete only common-binary functions and their single-use helpers:
   - `install_scip`
   - `install_zoekt`
   - `install_ctags`
   - `link_universal_ctags`
   - `install_jarvis_mcp`
   - `installed_scip_matches_pin`
   - scip/zoekt asset-name helpers used only by those installers.
3. Retain:
   - `install_scip_swift`
   - `install_npm_indexer`
   - `install_scip_typescript`
   - `install_scip_python`
   - `install_scip_java`
   - `install_bash_shim`
   - shared logging, checksum, download, platform, and shim helpers they use.
4. Replace usage prose with:

```text
Installs optional language indexers that are not bundled with jarvis's
Homebrew distribution. jarvis, scip, zoekt, and universal-ctags are installed
by: brew install jarvis-intelligence/jarvis/jarvis
```

5. In `parse_args`, replace the accepted `--only` values with this case:

```sh
case "$2" in
scip-swift | scip-typescript | scip-python | scip-java | bash-shim)
    ONLY=$2
    ;;
*)
    log_error "--only must be one of: scip-swift, scip-typescript, scip-python, scip-java, bash-shim"
    return 1
    ;;
esac
```

6. Replace main orchestration with:

```sh
	if should_run scip-swift; then run_one scip-swift install_scip_swift "$OS" "$ARCH"; fi
	if should_run scip-typescript; then run_one scip-typescript install_scip_typescript; fi
	if should_run scip-python; then run_one scip-python install_scip_python; fi
	if should_run scip-java; then run_one scip-java install_scip_java; fi
	if should_run bash-shim; then install_bash_shim "$OS"; fi
```

Preserve POSIX `sh` compatibility, `JARVIS_SETUP_SOURCED=1`, and the summary/failure behavior.

- [ ] **Step 4: Run setup and unit suites**

```bash
sh -n setup.sh
uv run pytest tests/test_setup_sh.py -q
uv run pytest -m "not integration" -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add setup.sh tests/test_setup_sh.py
git commit -m "chore(setup): keep only language indexer installers"
```

---

### Task 13: Documentation and release runbook cutover

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `.claude/skills/jarvis-release/SKILL.md`
- Modify: `docs/codebase-summary.md`
- Modify: `docs/project-overview-pdr.md`

**Interfaces:**
- Produces Homebrew-only user installation instructions.
- Produces a maintainer release runbook driven by `publish-native.yml`.

- [ ] **Step 1: Update README**

Replace the uv install section with:

**1. Install the standalone binary:**

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

State explicitly:

- no Python, uv, pip, or PyPI is required;
- `jarvis` and `jarvis-server` install together;
- SCIP, Zoekt indexing, Zoekt search, dashboard assets, tree-sitter, and `watch` are included;
- Homebrew installs `universal-ctags`;
- optional language indexers still need their own toolchains and `setup.sh --only ...`;
- semantic search is unavailable in this distribution;
- Linux users install Homebrew/Linuxbrew first.

Remove current-process PyPI badges, `uv tool install`, `uvx`, the MCP Registry ownership marker, and setup instructions for common SCIP/Zoekt/ctags binaries.

- [ ] **Step 2: Update changelog and runbook**

Add this changelog entry at the top under `Unreleased`:

```markdown
### Changed

- Replaced the uv/PyPI installation path with a Homebrew standalone
  distribution for macOS arm64/x86_64 and Linux arm64/x86_64.
- Bundled Python 3.12, `watch`, dashboard assets, tree-sitter libraries,
  pinned SCIP, Zoekt indexing, and Zoekt search binaries.
- Excluded optional semantic dependencies from the standalone distribution.
- Removed PyPI, MCP Registry, and legacy bootstrap publication workflows.
```

Replace the release runbook sequence with:

1. bump `pyproject.toml`;
2. run `uv run pytest -m "not integration" -rs`;
3. update `uv.lock` with `uv lock`;
4. merge the release PR;
5. tag and publish the GitHub Release;
6. confirm all four `publish-native.yml` build jobs pass;
7. confirm all four Homebrew validation jobs pass;
8. confirm the public tap receives the formula commit;
9. locally run `brew install jarvis-intelligence/jarvis/jarvis && jarvis --version`.

Remove PyPI trusted publishing, `server.json`, MCP Registry, and wheel-upload instructions. Update stale distribution prose in `docs/codebase-summary.md` and `docs/project-overview-pdr.md`.

- [ ] **Step 3: Verify current docs contain no legacy install path**

```bash
grep -RInE 'uv tool install|uvx --from jarvis-mcp|publish-pypi|publish-mcp-registry|sync-public-distribution' \
  README.md CHANGELOG.md docs .claude/skills/jarvis-release || true
uv run pytest -m "not integration" -q
uv run python scripts/check_versions.py
```

Expected: references appear only in explicitly historical changelog entries, if at all; tests and checker PASS.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md .claude/skills/jarvis-release/SKILL.md \
  docs/codebase-summary.md docs/project-overview-pdr.md
git commit -m "docs: document Homebrew standalone distribution"
```

---

### Task 14: Final verification and release readiness

**Files:**
- No source changes are planned.

**Interfaces:**
- Consumes the complete implementation.

- [ ] **Step 1: Run every local gate**

```bash
uv sync --extra semantic --group native
uv run pytest -m "not integration" -rs
uv run pytest tests/test_setup_sh.py -q
uv run python scripts/check_versions.py
sh -n setup.sh
bash -n scripts/ci_native_build.sh
uv run python scripts/check_native_package.py --help >/dev/null
uv run python scripts/fetch_native_binaries.py --help >/dev/null
uv run python scripts/generate_homebrew_formula.py --help >/dev/null
ruby -ryaml -e 'from = Dir[".github/workflows/*.yml"]; from.each { |f| YAML.safe_load(File.read(f)) }; puts "workflows parse"'
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Repeat the local darwin/arm64 proof**

Repeat every Task 10 command. Expected result is `native smoke: PASS` with no tracked source changes.

- [ ] **Step 3: Verify surface replacement**

```bash
test ! -e .github/workflows/publish-pypi.yml
test ! -e .github/workflows/publish-mcp-registry.yml
test ! -e .github/workflows/sync-public-distribution.yml
test ! -e server.json
test -e .github/workflows/publish-native.yml
grep -q 'brew install jarvis-intelligence/jarvis/jarvis' README.md
git status --short
```

Expected: every command exits 0; only pre-existing unrelated untracked files remain.

- [ ] **Step 4: Create the one-time public tap**

If it does not already exist:

```bash
cd "$(mktemp -d)"
gh repo create jarvis-intelligence/homebrew-jarvis \
  --public --clone --description "Homebrew formula for jarvis"
cd homebrew-jarvis
mkdir Formula
touch Formula/.gitkeep
git add Formula/.gitkeep
git commit -m "chore: initialize formula directory"
git push
```

Then create a fine-grained personal access token with metadata read and contents read/write on `jarvis-intelligence/homebrew-jarvis`, and add it as the private jarvis repository secret `JARVIS_TAP_TOKEN`.

- [ ] **Step 5: Report release readiness**

Report:

- local unit test result;
- local native smoke result;
- workflow parse result;
- public tap repository URL;
- whether `JARVIS_TAP_TOKEN` is configured;
- exact next command: publish a GitHub Release for the bumped version.

Do not claim the Homebrew release is complete until a real `publish-native.yml` run and local Homebrew install pass.

## Verification Summary

- Unit gate: `uv run pytest -m "not integration" -rs`.
- Packaging gate: fetcher, stager, checker, launcher, formula generator, and smoke-helper tests.
- Native gate: extracted CLI/MCP/index/search/watch runs without developer Python or uv.
- Homebrew gate: four architectures install, link both command names, and pass `brew test`.
- Publication gate: formula is committed only after all builds and Homebrew validations pass.
- Cleanup gate: PyPI, MCP Registry, legacy sync, `uv tool`, and end-user `uvx` paths are gone.
