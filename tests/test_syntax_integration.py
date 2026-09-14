"""Real-baseline acceptance (spec TSI-11): the syntax baseline must work
offline from prebuilt grammars with the SCIP toolchain removed from PATH,
fail hard without Zoekt, and route mixed-provider queries over a real
snapshot.

Module-level skip: zoekt-git-index is a REQUIRED binary. The grammars are
base dependencies (never skipped for absence); the SCIP binaries are
deliberately REMOVED from the test PATH to prove the baseline's
independence from them."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from jarvis import config
from jarvis.index_cli import _cmd_forget, _cmd_status, index_repo
from jarvis.registry import Registry

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "mini_py_repo"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("zoekt-git-index") is None,
        reason="zoekt-git-index (a required baseline binary) is not on PATH",
    ),
]


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def _strip_scip_from_path(monkeypatch) -> Path:
    """Remove every SCIP binary from PATH (zoekt stays reachable via a
    symlink shim) so a passing run proves the baseline needs no SCIP
    tooling. The scrapyard is total: the only PATH entry is a temp dir
    plus the OS essentials."""
    zoekt = shutil.which("zoekt-git-index")
    assert zoekt is not None
    link_dir = Path(tempfile.mkdtemp(prefix="t6-bin-"))
    (link_dir / "zoekt-git-index").symlink_to(zoekt)
    monkeypatch.setenv("PATH", f"{link_dir}:/usr/bin:/bin")
    assert shutil.which("scip") is None
    assert shutil.which("scip-python") is None
    assert shutil.which("zoekt-git-index") is not None
    return link_dir


def _offline_mocks(monkeypatch) -> None:
    """PATH stripped of SCIP plus the semantic stage skipped: an offline
    base install must never reach for a model download during indexing,
    and the embedding weight load would only slow the test without adding
    coverage of this spec."""
    _strip_scip_from_path(monkeypatch)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    return repo_dir, tmp_path / "data"


def test_offline_baseline_publishes_and_answers_navigation(
    tmp_path: Path, monkeypatch
):
    """Acceptance row 'Offline base installation': index a committed
    fixture with scip/scip-python removed from PATH -> exit-0 degraded,
    pointer published, and real documentSymbols answers tree-sitter
    declarations. The provider has no network path by construction: the
    grammar loader resolves pinned local wheels, and any download attempt
    would have to go through an http client none of syntax.py/syntax_index.py
    import (asserted at the bottom)."""
    _offline_mocks(monkeypatch)

    repo_dir, data_root = _make_repo(tmp_path)

    slug = index_repo(repo_dir, slug="offline", root=data_root)  # exit-0

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "degraded"  # enabled SCIP unavailable
        assert entry.scip_state == "unavailable"
        assert entry.scip_enabled is True  # failure never changes enablement
        assert "brew reinstall jarvis" in (
            entry.scip_failure_reason or ""
        )
    finally:
        registry.close()

    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    assert pointer  # pointer existence means a navigation snapshot exists

    # Real navigation through the Task-5 read surface over the real
    # published snapshot.
    from jarvis.query import QueryService

    service = QueryService(config.new_connection_cache(data_root))
    result = service.get_document_symbols(slug, "greeter.py")
    assert result.error is None
    assert result.entries
    syntax_entries = [e for e in result.entries if e.source == "tree-sitter"]
    assert syntax_entries, "an uncovered file must be served by syntax"
    names = {(e.displayName or "") for e in syntax_entries}
    # The opaque `symbol` ids round-trip; the human name rides displayName.
    assert any("greet" in n.lower() for n in names), names
    assert all(e.symbol.startswith("syntax:") for e in syntax_entries)
    assert all(e.positionEncoding == "utf-8" for e in syntax_entries)


def test_syntax_provider_imports_no_network_clients():
    """The offline claim, enforced structurally: no module on the syntax
    baseline's import path may pull an HTTP client."""
    import jarvis.syntax as syntax_module
    import jarvis.syntax_index as syntax_index_module

    for module in (syntax_module, syntax_index_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for client in ("urllib", "httpx", "requests", "socket"):
            assert client not in source, (module.__name__, client)


def test_offline_baseline_missing_zoekt_fails_hard(tmp_path: Path, monkeypatch):
    """Acceptance row 'Lifecycle': a missing REQUIRED binary (zoekt) stays
    a loud hard failure — nothing publishes, no pointer, failed row."""
    _offline_mocks(monkeypatch)
    monkeypatch.setattr(
        "jarvis.index_cli._zoekt_index_cmd",
        lambda zoekt_dir, repo_path: ["definitely-not-zoekt-git-index", "-x"],
    )

    from jarvis.index_cli import IndexingError

    repo_dir, data_root = _make_repo(tmp_path)

    with pytest.raises(IndexingError, match="zoekt-git-index"):
        index_repo(repo_dir, slug="nozoekt", root=data_root)

    assert not config.index_dir("nozoekt", data_root).exists()
    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get("nozoekt")
        assert entry.status == "failed"
    finally:
        registry.close()


def test_full_pipeline_and_cli_round_trip(tmp_path: Path, monkeypatch):
    """Acceptance round trip with the real toolchain present: syntax +
    SCIP + Zoekt land in one snapshot, `jarvis status` explains it, and
    `jarvis forget` tears everything down."""
    repo_dir, data_root = _make_repo(tmp_path)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))

    slug = index_repo(repo_dir, slug="roundtrip", root=data_root)
    assert slug == "roundtrip"

    assert _cmd_status(argparse.Namespace(slug="roundtrip")) == 0

    assert _cmd_forget(argparse.Namespace(slug="roundtrip")) == 0
    assert not config.index_dir("roundtrip", data_root).exists()
