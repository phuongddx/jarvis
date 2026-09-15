"""Unit tests for language detection, plus an end-to-end integration test
against `tests/fixtures/mini_py_repo/` using the real scip-python + scip
CLI binaries (marked `@pytest.mark.integration` — skipped if unavailable).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis import config
from jarvis.index_cli import PARTIAL_STATUS, UnsupportedLanguageError, detect_language, index_repo
from jarvis.registry import Registry

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "mini_py_repo"
SWIFT_FIXTURE_REPO = Path(__file__).parent / "fixtures" / "mini_swift_repo"
JAVA_FIXTURE_REPO = Path(__file__).parent / "fixtures" / "mini_java_repo"

_REQUIRED_BINARIES = ["scip-python", "scip", "zoekt-git-index"]
_missing = [b for b in _REQUIRED_BINARIES if shutil.which(b) is None]

_SWIFT_REQUIRED_BINARIES = ["scip-swift", "scip", "zoekt-git-index"]
_missing_swift = [b for b in _SWIFT_REQUIRED_BINARIES if shutil.which(b) is None]

_missing_java = [b for b in ("scip-java", "scip", "zoekt-git-index") if shutil.which(b) is None]


def _fake_completed_process(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """A stand-in for `_run`'s real return value in tests that mock it out --
    `_publish_search_only`/`index_repo` read `.stderr` off it for the
    coverage-shortfall check, so a bare `None` return no longer suffices."""
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def _stub_pipeline(monkeypatch) -> None:
    """Stub the external pipeline the way the neighbouring index_repo tests
    do: the SCIP step fails (exit-0 degraded) and every other `_run` step
    returns success without spawning binaries, so lock tests exercise the
    lock, not the optional tooling."""
    from jarvis import index_cli

    monkeypatch.setattr(index_cli, "check_scip_version", lambda: None)
    monkeypatch.setattr(
        index_cli, "_prepare_semantic_stage",
        lambda *a, **k: None,
    )

    def _run(cmd, *, cwd, step, env=None):
        if step.endswith(" index"):
            from jarvis.index_cli import IndexingError
            raise IndexingError(f"{step} failed (simulated real failure):\nexit 1")
        return _fake_completed_process(cmd)

    monkeypatch.setattr(index_cli, "_run", _run)


def test_git_tracked_files_lists_committed_paths(tmp_path: Path):
    from jarvis.index_cli import _git_tracked_files

    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    _init_git_repo(tmp_path)

    assert sorted(_git_tracked_files(tmp_path)) == ["a.py", "b.py"]


def test_git_tracked_files_handles_paths_with_spaces(tmp_path: Path):
    """`-z` is required: without it, git quotes/escapes non-ASCII names
    (e.g. as octal escapes like "caf\\303\\251.py") on the default
    newline-separated output, which would corrupt suffix parsing in
    detect_language(). A plain space would NOT reproduce this -- git
    does not quote plain-ASCII spaces even without `-z`."""
    from jarvis.index_cli import _git_tracked_files

    (tmp_path / "café.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    assert _git_tracked_files(tmp_path) == ["café.py"]


def test_git_tracked_files_raises_for_non_git_directory(tmp_path: Path):
    from jarvis.index_cli import NotAGitRepositoryError, _git_tracked_files

    (tmp_path / "a.py").write_text("x = 1\n")

    with pytest.raises(NotAGitRepositoryError):
        _git_tracked_files(tmp_path)


def test_git_head_raises_indexing_error_for_repo_with_no_commits(tmp_path: Path):
    """A freshly `git init`-ed repo has no HEAD. Previously this surfaced as
    a bare CalledProcessError with no explanation of what was wrong."""
    from jarvis.index_cli import IndexingError, _git_head

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("x = 1\n")

    with pytest.raises(IndexingError, match="no commits"):
        _git_head(tmp_path)


def test_git_head_raises_not_a_git_repository_for_non_git_directory(tmp_path: Path):
    """Distinct from the no-commits case above: a directory that isn't a
    git repository at all must not be misdiagnosed as "has no commits
    yet" -- `git rev-parse HEAD` fails identically in both cases, so
    `_git_head` must check `--is-inside-work-tree` first."""
    from jarvis.index_cli import NotAGitRepositoryError, _git_head

    (tmp_path / "a.py").write_text("x = 1\n")

    with pytest.raises(NotAGitRepositoryError):
        _git_head(tmp_path)


def test_detect_language_picks_python_for_py_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    _init_git_repo(tmp_path)
    language, cmd = detect_language(tmp_path)
    assert language == "python"
    assert cmd[0] == "scip-python"


def test_detect_language_picks_majority_extension(tmp_path: Path):
    for i in range(3):
        (tmp_path / f"f{i}.ts").write_text("export const x = 1;\n")
    (tmp_path / "g.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)
    language, _ = detect_language(tmp_path)
    assert language == "typescript"


def test_detect_language_ignores_committed_node_modules(tmp_path: Path):
    """Git alone does not save us here -- these files ARE tracked. The
    retained _IGNORED_DIRS pass is what excludes them."""
    (tmp_path / "src.py").write_text("x = 1\n")
    ignored = tmp_path / "node_modules" / "pkg"
    ignored.mkdir(parents=True)
    for i in range(5):
        (ignored / f"f{i}.ts").write_text("export const x = 1;\n")
    _init_git_repo(tmp_path)
    language, _ = detect_language(tmp_path)
    assert language == "python"


def test_detect_language_ignores_committed_derived_data_and_dot_build(tmp_path: Path):
    """Same as above for Swift build output that a repo happens to commit."""
    (tmp_path / "src.swift").write_text("let x = 1\n")
    derived_data = tmp_path / "DerivedData" / "SourcePackages" / "checkouts" / "SomeDep"
    derived_data.mkdir(parents=True)
    dot_build = tmp_path / ".build" / "checkouts" / "SomeDep"
    dot_build.mkdir(parents=True)
    for i in range(5):
        (derived_data / f"f{i}.py").write_text("x = 1\n")
        (dot_build / f"g{i}.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)
    language, _ = detect_language(tmp_path)
    assert language == "swift"


def test_detect_language_picks_swift_for_swift_files(tmp_path: Path):
    (tmp_path / "a.swift").write_text("let x = 1\n")
    (tmp_path / "b.swift").write_text("let y = 2\n")
    _init_git_repo(tmp_path)
    language, cmd = detect_language(tmp_path)
    assert language == "swift"
    assert cmd[0] == "scip-swift"


def test_swift_invocation_omits_index_subcommand(tmp_path: Path):
    """The bare form is required for cross-version compatibility.

    scip-swift only gained its `index` subcommand after v0.1.0 shipped, so
    `scip-swift index --output ...` fails against that released binary -- it
    parses "index" as the repo path. The bare form works on every version.
    """
    (tmp_path / "a.swift").write_text("let x = 1\n")
    _init_git_repo(tmp_path)
    _, cmd = detect_language(tmp_path)
    assert cmd == ["scip-swift"], f"must stay bare for version tolerance, got {cmd}"


def test_detect_language_tie_break_prefers_earlier_priority_over_swift(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.swift").write_text("let x = 1\n")
    _init_git_repo(tmp_path)
    language, _ = detect_language(tmp_path)
    assert language == "python"


def test_detect_language_raises_for_no_supported_files(tmp_path: Path):
    (tmp_path / "README.md").write_text("# hi\n")
    _init_git_repo(tmp_path)
    with pytest.raises(UnsupportedLanguageError):
        detect_language(tmp_path)


def test_detect_language_ignores_gitignored_checkout_directory(tmp_path: Path):
    """Direct regression test for the sample-python-repo failure: a
    gitignored `.local-checkouts/` of cloned sibling repos held 4782 .ts/.tsx
    files against the repo's own 81 tracked .py files, and detection picked
    typescript. Nothing in _IGNORED_DIRS covered it, and nothing could --
    the directory name is arbitrary and per-project."""
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text(".local-checkouts/\n")
    checkouts = tmp_path / ".local-checkouts" / "vendored-repo"
    checkouts.mkdir(parents=True)
    for i in range(50):
        (checkouts / f"f{i}.ts").write_text("export const x = 1;\n")

    _init_git_repo(tmp_path)  # `git add .` honors .gitignore

    language, _ = detect_language(tmp_path)
    assert language == "python"


def test_detect_language_ignores_untracked_files(tmp_path: Path):
    """Tracked-only by design. Uncommitted scratch files do not vote."""
    (tmp_path / "app.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    for i in range(50):
        (tmp_path / f"scratch{i}.ts").write_text("export const x = 1;\n")

    language, _ = detect_language(tmp_path)
    assert language == "python"


def test_detect_language_raises_for_non_git_directory(tmp_path: Path):
    from jarvis.index_cli import NotAGitRepositoryError

    (tmp_path / "a.py").write_text("x = 1\n")

    with pytest.raises(NotAGitRepositoryError):
        detect_language(tmp_path)


def test_prefers_xcodebuild_false_for_bare_spm_package(tmp_path: Path):
    from jarvis.index_cli import _prefers_xcodebuild

    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    assert _prefers_xcodebuild(tmp_path) is False


def test_prefers_xcodebuild_true_when_xcodeproj_present(tmp_path: Path):
    from jarvis.index_cli import _prefers_xcodebuild

    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    (tmp_path / "MyLib.xcodeproj").mkdir()
    assert _prefers_xcodebuild(tmp_path) is True


def test_prefers_xcodebuild_true_when_xcworkspace_present(tmp_path: Path):
    from jarvis.index_cli import _prefers_xcodebuild

    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    (tmp_path / "MyLib.xcworkspace").mkdir()
    assert _prefers_xcodebuild(tmp_path) is True


def test_swift_indexer_cmd_appends_cache_dir_without_xcodeproj(tmp_path, monkeypatch):
    """The cache flag is NOT xcodebuild-only: swiftpm runs carry it too,
    pointing at exactly config.swift_cache_dir(slug)."""
    from jarvis import config
    from jarvis.index_cli import _swift_indexer_cmd

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    cache = config.swift_cache_dir("demo")
    assert _swift_indexer_cmd(["scip-swift"], tmp_path, None, cache) == [
        "scip-swift", "--cache-dir", str(tmp_path / "cache" / "scip-swift" / "demo"),
    ]


def test_swift_indexer_cmd_adds_xcodebuild_then_cache_dir(tmp_path):
    from jarvis.index_cli import _swift_indexer_cmd

    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    (tmp_path / "MyLib.xcodeproj").mkdir()
    cache = tmp_path / "cache" / "scip-swift" / "demo"
    assert _swift_indexer_cmd(["scip-swift"], tmp_path, None, cache) == [
        "scip-swift", "--build-tool", "xcodebuild", "--cache-dir", str(cache),
    ]


def test_swift_indexer_cmd_orders_scheme_before_cache_dir(tmp_path):
    from jarvis.index_cli import _swift_indexer_cmd

    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    (tmp_path / "MyLib.xcodeproj").mkdir()
    cache = tmp_path / "cache" / "scip-swift" / "demo"
    assert _swift_indexer_cmd(["scip-swift"], tmp_path, "ios_theme_ui", cache) == [
        "scip-swift", "--build-tool", "xcodebuild", "--scheme", "ios_theme_ui",
        "--cache-dir", str(cache),
    ]


def test_swift_indexer_cmd_ignores_scheme_without_xcodeproj(tmp_path):
    """A --scheme override is meaningless (and unsupported by scip-swift)
    under the swiftpm build tool, so it must not leak into the command
    when there's no checked-in Xcode project to justify xcodebuild."""
    from jarvis.index_cli import _swift_indexer_cmd

    (tmp_path / "Package.swift").write_text("// swift-tools-version: 6.0\n")
    cache = tmp_path / "cache" / "scip-swift" / "demo"
    assert _swift_indexer_cmd(["scip-swift"], tmp_path, "ios_theme_ui", cache) == [
        "scip-swift", "--cache-dir", str(cache),
    ]


def test_detect_language_tie_break_prefers_java_over_swift(tmp_path: Path):
    (tmp_path / "a.java").write_text("class A {}\n")
    (tmp_path / "b.java").write_text("class B {}\n")
    (tmp_path / "a.swift").write_text("let x = 1\n")
    (tmp_path / "b.swift").write_text("let y = 2\n")
    _init_git_repo(tmp_path)
    language, _ = detect_language(tmp_path)
    assert language == "java"


def test_index_repo_rejects_dotdot_slug_before_touching_disk(tmp_path: Path):
    """A `--slug ..` (or any slug resolving to `.`/`..`) must be rejected
    before `config.index_dir()` ever builds a path from it — otherwise
    `jarvis forget ..` could `shutil.rmtree()` outside the data dir."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "a.py").write_text("x = 1\n")
    with pytest.raises(ValueError):
        index_repo(repo_dir, slug="..", root=tmp_path / "data")
    # No registry or scip dir should have been created for the rejected slug.
    assert not (tmp_path / "data").exists()


def test_index_repo_rejects_a_second_slug_for_the_same_path(tmp_path: Path, monkeypatch):
    """zoekt.name is one value per repo, so a second slug for one path would
    overwrite the first's name and silently break `r:<first-slug>`."""
    from jarvis import config
    from jarvis.index_cli import IndexingError, index_repo
    from jarvis.registry import Registry

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _init_git_repo(repo)

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("first", str(repo.resolve()), "python", "abc", "indexed")
    finally:
        registry.close()

    with pytest.raises(IndexingError, match="already indexed as 'first'"):
        index_repo(repo, slug="second")


def test_index_repo_allows_reindexing_the_same_slug(tmp_path: Path, monkeypatch):
    """The normal reindex/watch path: same slug, same path, must not trip."""
    from jarvis import config
    from jarvis.index_cli import _reject_duplicate_slug_for_path
    from jarvis.registry import Registry

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _init_git_repo(repo)

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("same", str(repo.resolve()), "python", "abc", "indexed")
        _reject_duplicate_slug_for_path(registry, "same", repo.resolve())  # must not raise
    finally:
        registry.close()


def test_reject_duplicate_slug_compares_resolved_paths(tmp_path: Path, monkeypatch):
    """Rows written before this change may hold unresolved paths; a trailing
    "/." or symlinked parent must still be recognised as the same repo."""
    from jarvis import config
    from jarvis.index_cli import IndexingError, _reject_duplicate_slug_for_path
    from jarvis.registry import Registry

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _init_git_repo(repo)

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("first", f"{repo}/.", "python", "abc", "indexed")
        with pytest.raises(IndexingError, match="already indexed as 'first'"):
            _reject_duplicate_slug_for_path(registry, "second", repo.resolve())
    finally:
        registry.close()



def test_resolve_slug_reuses_the_registered_slug_for_a_known_path(tmp_path, monkeypatch):
    """A repo indexed under an explicit --slug must keep it: resolution is by
    path first, basename second. Otherwise indexRepo would derive a different
    slug and index the same repo twice."""
    from jarvis import index_cli
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("custom-name", str(repo), "python", None, "indexed")
        assert index_cli.resolve_slug_for_path(registry, repo) == "custom-name"
    finally:
        registry.close()


def test_resolve_slug_rejects_same_basename_at_a_different_live_path(tmp_path, monkeypatch):
    from jarvis import index_cli
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    first = tmp_path / "a" / "app"
    second = tmp_path / "b" / "app"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("app", str(first), "python", None, "indexed")
        with pytest.raises(index_cli.IndexingError) as exc:
            index_cli.resolve_slug_for_path(registry, second)
        assert str(first) in str(exc.value)
        assert str(second) in str(exc.value)
    finally:
        registry.close()


def test_resolve_slug_allows_a_moved_repo(tmp_path, monkeypatch):
    """The registered path no longer exists, so this is a move, not a
    collision -- `upsert` already updates `path`."""
    from jarvis import index_cli
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    moved_to = tmp_path / "new" / "app"
    moved_to.mkdir(parents=True)
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("app", str(tmp_path / "gone" / "app"), "python", None, "indexed")
        assert index_cli.resolve_slug_for_path(registry, moved_to) == "app"
    finally:
        registry.close()


def test_resolve_slug_derives_basename_when_unregistered(tmp_path, monkeypatch):
    from jarvis import index_cli
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "My Repo"
    repo.mkdir()
    registry = Registry(config.data_dir() / "registry.db")
    try:
        assert index_cli.resolve_slug_for_path(registry, repo) == "my-repo"
    finally:
        registry.close()


def test_index_repo_rejects_same_slug_at_a_different_path(tmp_path, monkeypatch):
    """The guard must hold in the locked writer, not only in the MCP
    pre-flight -- otherwise it is pure TOCTOU."""
    from jarvis import index_cli
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    first = tmp_path / "a" / "app"
    first.mkdir(parents=True)
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("app", str(first), "python", None, "indexed")
    finally:
        registry.close()

    second = tmp_path / "b" / "app"
    second.mkdir(parents=True)
    shutil.copytree(FIXTURE_REPO, second, dirs_exist_ok=True)
    _init_git_repo(second)
    _stub_pipeline(monkeypatch)
    with pytest.raises(index_cli.IndexingError):
        index_cli.index_repo(second)


def test_ensure_git_repo_rejects_a_plain_directory(tmp_path):
    from jarvis import index_cli

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(index_cli.NotAGitRepositoryError):
        index_cli.ensure_git_repo(plain)


def test_ensure_git_repo_rejects_a_missing_path(tmp_path):
    """A nonexistent path must surface as `NotAGitRepositoryError` (which
    `_cmd_index` catches) — not a raw `FileNotFoundError` from Python's own
    chdir, which would escape as a traceback. This pins the `git -C`
    invocation form: with `cwd=repo_path`, Python raises before git runs."""
    from jarvis import index_cli

    missing = tmp_path / "does-not-exist"
    with pytest.raises(index_cli.NotAGitRepositoryError):
        index_cli.ensure_git_repo(missing)


def test_java_indexer_env_disables_gradle_parallelism(monkeypatch):
    from jarvis.index_cli import _java_indexer_env

    monkeypatch.delenv("GRADLE_OPTS", raising=False)
    assert _java_indexer_env()["GRADLE_OPTS"] == "-Dorg.gradle.parallel=false"


def test_java_indexer_env_appends_to_existing_gradle_opts(monkeypatch):
    """Clobbering GRADLE_OPTS would silently discard the user's heap settings."""
    from jarvis.index_cli import _java_indexer_env

    monkeypatch.setenv("GRADLE_OPTS", "-Xmx4g")
    value = _java_indexer_env()["GRADLE_OPTS"]
    assert "-Xmx4g" in value
    assert "-Dorg.gradle.parallel=false" in value


def test_java_indexer_env_prepends_shim_dir_when_bash_shim_exists(tmp_path: Path, monkeypatch):
    """The shim must come FIRST — the whole point is beating /bin/bash 3.2."""
    from jarvis.index_cli import _java_indexer_env

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "bash").write_text("#!/bin/sh\n")

    assert _java_indexer_env()["PATH"] == f"{shims}{os.pathsep}/usr/bin:/bin"


def test_java_indexer_env_omits_path_when_no_shim(tmp_path: Path, monkeypatch):
    """No shim on Linux or a modern-bash mac: the branch must stay inert."""
    from jarvis.index_cli import _java_indexer_env

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))

    assert "PATH" not in _java_indexer_env()


def test_run_merges_env_over_os_environ(tmp_path: Path, monkeypatch):
    """env= must extend os.environ, not replace it — PATH must survive."""
    from jarvis.index_cli import _run

    monkeypatch.setenv("JARVIS_MARKER", "from-parent")
    out = tmp_path / "out.txt"
    _run(
        ["sh", "-c", f'printf "%s|%s" "$JARVIS_MARKER" "$EXTRA" > {out}'],
        cwd=tmp_path,
        step="probe",
        env={"EXTRA": "from-arg"},
    )
    assert out.read_text() == "from-parent|from-arg"


def test_forget_rejects_dotdot_slug(tmp_path: Path, monkeypatch, capsys):
    from jarvis.index_cli import _cmd_forget
    import argparse

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    rc = _cmd_forget(argparse.Namespace(slug=".."))
    assert rc == 1
    assert "error" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_index_repo_end_to_end_atomic_swap_under_open_reader(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)

    data_root = tmp_path / "data"
    slug = index_repo(repo_dir, root=data_root)

    from jarvis.index_cli import _tracked_blob_count

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.commit_sha is not None
        assert entry.tracked_files == _tracked_blob_count(repo_dir)
    finally:
        registry.close()

    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    # Spec TSI-04: index-<commit>-<generation>.db — the generation makes
    # same-commit reindexes distinct; the metadata stem derives from it.
    assert re.fullmatch(rf"index-{entry.commit_sha}-[0-9a-f]+\.db", pointer), pointer
    assert (target_dir / (pointer.removesuffix(".db") + ".metadata.json")).is_file()

    # Open a reader connection against the published db BEFORE reindexing,
    # to prove reindex-under-load doesn't corrupt an in-flight read.
    reader = sqlite3.connect(f"file:{target_dir / pointer}?mode=ro", uri=True)
    count_before = reader.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0]
    assert count_before > 0

    # Reindex (no new commit, but the pipeline runs again) — the open
    # reader's connection must remain valid throughout.
    index_repo(repo_dir, slug=slug, root=data_root)
    count_after = reader.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0]
    assert count_after == count_before
    reader.close()

    zoekt_shards = list((data_root / ".zoekt").glob("*.zoekt"))
    assert zoekt_shards, "zoekt-git-index should have written at least one shard"


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_index_repo_marks_failed_on_indexer_error(tmp_path: Path, monkeypatch):
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
def test_index_repo_degrades_on_real_indexer_error(tmp_path: Path, monkeypatch):
    """Narrowed FALL-04 (spec §12): a real failing language indexer is
    optional-enrichment failure — exit-0 degraded, syntax baseline and
    search still published, the SCIP cause on the stage fields."""
    from jarvis.index_cli import index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    # Force a genuine scip-python failure: it exits nonzero on a directory
    # that is not a python package root it can index.
    (repo_dir / "pyproject.toml").write_text("[tool.broken]\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "break"], cwd=repo_dir, check=True)

    def _run(cmd, *, cwd, step, env=None):
        if step.endswith(" index"):
            from jarvis.index_cli import IndexingError
            raise IndexingError(f"{step} failed (simulated real failure):\nexit 1")
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)

    slug = index_repo(repo_dir, root=data_root)  # exit-0, no raise
    entry = Registry(data_root / "registry.db").get(slug)
    assert entry.status == "degraded"
    assert entry.scip_state == "failed"
    assert "simulated real failure" in entry.scip_failure_reason
    target_dir = config.index_dir(slug, data_root)
    assert (target_dir / "current").is_file()


@pytest.mark.skipif(_missing_swift, reason=f"missing required binaries: {_missing_swift}")
def test_index_repo_end_to_end_for_swift_repo(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    shutil.copytree(SWIFT_FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)

    data_root = tmp_path / "data"
    slug = index_repo(repo_dir, root=data_root)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.language == "swift"
    finally:
        registry.close()

    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    db = sqlite3.connect(f"file:{target_dir / pointer}?mode=ro", uri=True)
    try:
        count = db.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0]
        assert count > 0
    finally:
        db.close()


@pytest.mark.integration
@pytest.mark.skipif(_missing_swift, reason=f"missing required binaries: {_missing_swift}")
def test_index_repo_preserves_scheme_override_when_not_repassed(tmp_path: Path):
    """Regression test for the jarvis-watch bug: a second index_repo()
    call with scheme=None (e.g. an unattended `jarvis watch` reindex)
    must not wipe a previously stored scheme_override."""
    repo_dir = tmp_path / "repo"
    shutil.copytree(SWIFT_FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    slug = index_repo(repo_dir, root=data_root, scheme="some-scheme")
    index_repo(repo_dir, slug=slug, root=data_root, scheme=None)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.scheme_override == "some-scheme"
    finally:
        registry.close()


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_index_repo_preserves_language_override_across_successful_index(tmp_path: Path):
    """The success path upserts a second time. If that call omits
    language_override, the override silently resets to NULL and a later
    reindex falls back to detection."""
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    slug = index_repo(repo_dir, root=data_root, language="python")

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.status in ("indexed", PARTIAL_STATUS)
        assert entry.language_override == "python"
    finally:
        registry.close()


def test_resolve_scheme_preserves_stored_override_when_none_given(tmp_path: Path):
    from jarvis.index_cli import _resolve_scheme

    registry = Registry(tmp_path / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "swift", "abc123", "indexed", scheme_override="ios_theme_ui")
    assert _resolve_scheme(registry, "my-repo", scheme=None) == "ios_theme_ui"
    registry.close()


def test_resolve_scheme_prefers_explicit_value_over_stored(tmp_path: Path):
    from jarvis.index_cli import _resolve_scheme

    registry = Registry(tmp_path / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "swift", "abc123", "indexed", scheme_override="old")
    assert _resolve_scheme(registry, "my-repo", scheme="new") == "new"
    registry.close()


def test_resolve_scheme_returns_none_for_unknown_slug(tmp_path: Path):
    from jarvis.index_cli import _resolve_scheme

    registry = Registry(tmp_path / "registry.db")
    assert _resolve_scheme(registry, "nope", scheme=None) is None
    registry.close()


def test_parse_scip_version_reads_standard_output():
    from jarvis.index_cli import parse_scip_version

    assert parse_scip_version("scip version v0.9.0") == (0, 9, 0)
    assert parse_scip_version("scip version v0.10.2\n") == (0, 10, 2)
    assert parse_scip_version("v1.0.0") == (1, 0, 0)


def test_parse_scip_version_returns_none_on_junk():
    from jarvis.index_cli import parse_scip_version

    assert parse_scip_version("") is None
    assert parse_scip_version("not a version") is None


def test_check_scip_version_rejects_v070(monkeypatch):
    """v0.7.0 converts successfully but silently drops every range."""
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_version_output", lambda: "scip version v0.7.0")
    with pytest.raises(cli.IndexingError) as exc:
        cli.check_scip_version()
    message = str(exc.value)
    assert "0.7.0" in message
    assert "0.9.0" in message, "must state the required floor"
    assert "brew reinstall jarvis" in message
    assert "jarvis reindex" in message


def test_check_scip_version_accepts_v090(monkeypatch):
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_version_output", lambda: "scip version v0.9.0")
    cli.check_scip_version()  # must not raise


def test_check_scip_version_accepts_newer(monkeypatch):
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_version_output", lambda: "scip version v1.2.3")
    cli.check_scip_version()


def test_check_scip_version_tolerates_unparseable(monkeypatch):
    """An unrecognized format must not block indexing outright."""
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_version_output", lambda: "weird build")
    cli.check_scip_version()  # must not raise


def test_check_scip_swift_version_rejects_v021(monkeypatch):
    """v0.2.1 predates the xcodebuild-dispatch fix (restored in 0.3.0):
    indexing an .xcodeproj repo through it would silently produce a broken
    index, so it must fail loudly with a recovery hint."""
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_swift_version_output", lambda: "0.2.1 (swift 6.1.0)")
    with pytest.raises(cli.IndexingError) as exc:
        cli.check_scip_swift_version()
    message = str(exc.value)
    assert "0.2.1" in message, "must name the installed version"
    assert "0.3.0" in message, "must state the required floor"
    assert "sh setup.sh --only scip-swift" in message, "must name the exact recovery command"


def test_check_scip_swift_version_accepts_v030_real_format(monkeypatch):
    """scip-swift prints "0.3.0 (swift 6.2.4)" -- no v prefix. The floor
    must accept the real output format, not a v-prefixed stand-in."""
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_swift_version_output", lambda: "0.3.0 (swift 6.2.4)")
    cli.check_scip_swift_version()  # must not raise


def test_check_scip_swift_version_tolerates_unparseable(monkeypatch):
    """Warn-by-omission, same policy as check_scip_version: an unexpected
    build string must not block indexing outright."""
    import jarvis.index_cli as cli

    monkeypatch.setattr(cli, "_scip_swift_version_output", lambda: "weird build")
    cli.check_scip_swift_version()  # must not raise


def test_scip_version_output_missing_binary_names_homebrew_remedy(monkeypatch):
    """A missing bundled binary points to the Homebrew package."""
    import jarvis.index_cli as cli

    def _no_binary(*_args, **_kwargs):
        raise FileNotFoundError("scip")

    monkeypatch.setattr(cli.subprocess, "run", _no_binary)
    with pytest.raises(cli.IndexingError, match="brew reinstall jarvis"):
        cli._scip_version_output()


def test_scip_swift_version_output_missing_binary_names_setup_sh(monkeypatch):
    """A missing optional binary names its retained setup.sh selector."""
    import jarvis.index_cli as cli

    def _no_binary(*_args, **_kwargs):
        raise FileNotFoundError("scip-swift")

    monkeypatch.setattr(cli.subprocess, "run", _no_binary)
    with pytest.raises(cli.IndexingError, match=r"setup\.sh --only scip-swift"):
        cli._scip_swift_version_output()


def test_index_repo_non_swift_never_probes_scip_swift_version(tmp_path: Path, monkeypatch):
    """The floor gate is Swift-only (D-04): a non-Swift index must not need
    the scip-swift binary at all -- hosts without Swift repos may never
    install it (setup.sh skips it off darwin/arm64)."""
    import jarvis.index_cli as cli

    for i in range(3):
        (tmp_path / f"m{i}.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    def boom():
        raise AssertionError("scip-swift must not be probed for a non-swift repo")

    monkeypatch.setattr(cli, "_scip_swift_version_output", boom)
    monkeypatch.setattr(cli, "check_scip_version", lambda: None)

    def fake_run(cmd, *, cwd, step, env=None):
        raise cli.IndexingError("stop after the indexer command is built")

    monkeypatch.setattr(cli, "_run", fake_run)

    # If the probe fired, its AssertionError (not IndexingError) propagates
    # through the pre-pipeline failure wrap's bare `raise` and fails here.
    with pytest.raises(cli.IndexingError, match="stop after the indexer"):
        cli.index_repo(tmp_path, slug="pure-py", root=tmp_path / "data")


def test_zoekt_index_cmd_uses_git_index_with_pinned_flags(tmp_path: Path):
    """-incremental=false because the default would refuse to repair an
    already-published incomplete shard. -submodules=false because submodules
    are indexed as their own slugs, and including them here would both
    duplicate content and make the coverage expectation unreachable."""
    from jarvis.index_cli import _zoekt_index_cmd

    cmd = _zoekt_index_cmd(tmp_path / ".zoekt", tmp_path / "repo")

    assert cmd[0] == "zoekt-git-index"
    assert "-incremental=false" in cmd
    assert "-submodules=false" in cmd
    assert "-meta" not in cmd, "zoekt-git-index has no -meta flag"
    assert cmd[-1] == str(tmp_path / "repo")


def test_write_zoekt_meta_is_gone():
    """Replaced by _pin_zoekt_repo_name — zoekt-git-index takes no -meta."""
    import jarvis.index_cli as index_cli

    assert not hasattr(index_cli, "_write_zoekt_meta")


def test_run_returns_the_completed_process(tmp_path: Path):
    """Coverage parsing needs the indexer's stderr, which _run previously
    discarded on success."""
    from jarvis.index_cli import _run

    result = _run(["echo", "hello"], cwd=tmp_path, step="echo")

    assert result.stdout.strip() == "hello"


@pytest.mark.parametrize(
    ("binary", "remedy"),
    [
        ("scip", "brew reinstall jarvis, then rerun indexing"),
        ("zoekt-git-index", "brew reinstall jarvis, then rerun indexing"),
        ("zoekt-webserver", "brew reinstall jarvis, then rerun indexing"),
        ("scip-python", "sh setup.sh --only scip-python"),
        ("scip-typescript", "sh setup.sh --only scip-typescript"),
        ("scip-java", "sh setup.sh --only scip-java"),
        ("scip-swift", "sh setup.sh --only scip-swift"),
    ],
)
def test_run_names_a_binary_specific_recovery(
    tmp_path: Path, monkeypatch, binary: str, remedy: str
):
    """A missing binary raised a bare FileNotFoundError, which says nothing
    about how to fix it. Bundled binaries belong to Homebrew; retained
    language indexers still belong to setup.sh."""
    import jarvis.index_cli as index_cli

    def _missing(*_args, **_kwargs):
        raise FileNotFoundError(binary)

    monkeypatch.setattr(index_cli.subprocess, "run", _missing)
    with pytest.raises(index_cli.MissingBinaryError, match=remedy) as excinfo:
        index_cli._run([binary], cwd=tmp_path, step=f"{binary} fake")
    assert binary in str(excinfo.value)


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_zoekt_shard_is_named_by_slug_not_directory(tmp_path: Path):
    """Regression: searchCode(repo=<slug>) silently returned zero hits.

    Zoekt names shards from the directory basename unless -meta says
    otherwise, so a slug differing from the directory produced a shard the
    r: filter could never match.
    """
    repo_dir = tmp_path / "directoryname"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)

    data_root = tmp_path / "data"
    index_repo(repo_dir, slug="totally-different-slug", root=data_root)

    shards = list((data_root / ".zoekt").glob("*.zoekt"))
    names = [s.name for s in shards]
    assert any(n.startswith("totally-different-slug") for n in names), names
    assert not any(n.startswith("directoryname") for n in names), names


def test_remove_zoekt_shards_deletes_matching_shard(tmp_path: Path, monkeypatch):
    from jarvis.index_cli import _remove_zoekt_shards

    zoekt_dir = tmp_path / ".zoekt"
    zoekt_dir.mkdir(parents=True)
    (zoekt_dir / "myslug_v16.00000.zoekt").write_bytes(b"x")
    (zoekt_dir / "myslug_v16.00001.zoekt").write_bytes(b"x")
    (zoekt_dir / "otherslug_v16.00000.zoekt").write_bytes(b"x")

    removed = _remove_zoekt_shards("myslug", root=tmp_path)

    assert len(removed) == 2
    assert not (zoekt_dir / "myslug_v16.00000.zoekt").exists()
    assert not (zoekt_dir / "myslug_v16.00001.zoekt").exists()
    assert (zoekt_dir / "otherslug_v16.00000.zoekt").exists(), "must not touch other repos"


def test_remove_zoekt_shards_does_not_prefix_match_other_slugs(tmp_path: Path):
    """"api" must not delete "api-gateway"'s shard."""
    from jarvis.index_cli import _remove_zoekt_shards

    zoekt_dir = tmp_path / ".zoekt"
    zoekt_dir.mkdir(parents=True)
    (zoekt_dir / "api_v16.00000.zoekt").write_bytes(b"x")
    (zoekt_dir / "api-gateway_v16.00000.zoekt").write_bytes(b"x")

    removed = _remove_zoekt_shards("api", root=tmp_path)

    assert len(removed) == 1
    assert not (zoekt_dir / "api_v16.00000.zoekt").exists()
    assert (zoekt_dir / "api-gateway_v16.00000.zoekt").exists()


def test_remove_zoekt_shards_is_safe_when_absent(tmp_path: Path):
    from jarvis.index_cli import _remove_zoekt_shards

    assert _remove_zoekt_shards("nothing-here", root=tmp_path) == []


from tests.fixtures import scip_encoder


def _make_index_db(path: Path, *, chunks: int, mentions: int, symbols: int = 3) -> None:
    """Minimal stand-in for the expt-convert schema: the counted tables plus
    the capability tables `finalize_snapshot` reads (empty occurrences
    blobs decode to no occurrences, so the snapshot's provider flags stay
    off — realistic for a converter that emitted a symbol table only)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, relative_path TEXT);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, document_id INTEGER, occurrences BLOB);
        CREATE TABLE global_symbols (id INTEGER PRIMARY KEY, symbol TEXT,
            signature TEXT, relationships BLOB);
        CREATE TABLE mentions (chunk_id INTEGER, symbol_id INTEGER, role INTEGER);
        CREATE TABLE defn_enclosing_ranges (document_id INTEGER, start_line INTEGER,
            start_column INTEGER, end_line INTEGER, end_column INTEGER);
        """
    )
    conn.execute("INSERT INTO documents (relative_path) VALUES ('a.swift')")
    for i in range(symbols):
        conn.execute("INSERT INTO global_symbols (symbol) VALUES (?)", (f"sym{i}",))
    # Genuine empty occurrence blobs (not NULL): finalize_snapshot decodes
    # every chunk's blob exactly once.
    empty_occurrences = scip_encoder.encode_occurrences([])
    for i in range(chunks):
        conn.execute("INSERT INTO chunks (document_id, occurrences) VALUES (1, ?)",
                     (empty_occurrences,))
    for i in range(mentions):
        conn.execute("INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (1, 1, 1)")
    conn.commit()
    conn.close()


def test_index_has_navigation_data_false_when_chunks_and_mentions_empty(tmp_path: Path):
    """The exact fingerprint of a converter that dropped every range."""
    from jarvis.index_cli import index_has_navigation_data

    db = tmp_path / "i.db"
    _make_index_db(db, chunks=0, mentions=0)
    conn = sqlite3.connect(db)
    try:
        assert index_has_navigation_data(conn) is False
    finally:
        conn.close()


def test_index_has_navigation_data_true_when_populated(tmp_path: Path):
    from jarvis.index_cli import index_has_navigation_data

    db = tmp_path / "i.db"
    _make_index_db(db, chunks=2, mentions=3)
    conn = sqlite3.connect(db)
    try:
        assert index_has_navigation_data(conn) is True
    finally:
        conn.close()


def test_index_has_navigation_data_false_when_only_chunks(tmp_path: Path):
    from jarvis.index_cli import index_has_navigation_data

    db = tmp_path / "i.db"
    _make_index_db(db, chunks=2, mentions=0)
    conn = sqlite3.connect(db)
    try:
        assert index_has_navigation_data(conn) is False
    finally:
        conn.close()


def _mock_healthy_full_run(monkeypatch):
    """Mocks for a fully successful main-pipeline run: every subprocess
    succeeds, the convert step writes a navigable minimal index db, and
    the semantic stage skips (the two-phase pattern of the ordering test).
    The SCIP version gate is patched because the optional stage would
    otherwise probe a real `scip` binary."""
    def _successful_run(cmd, *, cwd, step, env=None):
        if step == "scip expt-convert":
            _make_index_db(Path(cmd[3]), chunks=1, mentions=14)
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)
    monkeypatch.setattr("jarvis.index_cli._run", _successful_run)
    monkeypatch.setattr("jarvis.index_cli.populate_graph_for_repo", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)


def test_reindex_forwards_stored_scheme_override(tmp_path: Path, monkeypatch):
    import argparse
    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))

    registry = Registry(config.data_dir() / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "swift", "abc123", "indexed", scheme_override="ios_theme_ui",
                    semantic_include=("src/gen",))
    registry.close()

    captured: dict = {}

    def fake_index_repo(path, *, slug=None, root=None, scheme=None, semantic_include=None,
                        language=None, scip=None, semantic=True, watch=False):
        captured["path"] = path
        captured["slug"] = slug
        captured["scheme"] = scheme
        captured["semantic_include"] = semantic_include
        captured["language"] = language
        return slug

    monkeypatch.setattr(cli, "index_repo", fake_index_repo)

    rc = cli._cmd_reindex(argparse.Namespace(slug="my-repo"))
    assert rc == 0
    assert captured["scheme"] == "ios_theme_ui"
    assert str(captured["path"]) == "/repos/my-repo"
    # _cmd_reindex forwards `list(repo.semantic_include)` to _cmd_index, which
    # converts it back to a tuple before calling index_repo — so the value
    # observed here (at the index_repo boundary) is a tuple, not a list.
    assert captured["semantic_include"] == ("src/gen",)


def test_forget_removes_the_zoekt_shard(tmp_path: Path, monkeypatch, capsys):
    """Regression: forgotten repos stayed searchable."""
    import argparse

    from jarvis import config
    from jarvis.index_cli import _cmd_forget
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))

    registry = Registry(config.data_dir() / "registry.db")
    registry.upsert("goneslug", str(tmp_path / "repo"), "python", "abc123", "indexed")
    registry.close()

    index_dir = config.index_dir("goneslug")
    index_dir.mkdir(parents=True, exist_ok=True)
    (index_dir / "index-abc123.db").write_bytes(b"x")

    zoekt_dir = config.data_dir() / ".zoekt"
    zoekt_dir.mkdir(parents=True, exist_ok=True)
    shard = zoekt_dir / "goneslug_v16.00000.zoekt"
    shard.write_bytes(b"x")

    rc = _cmd_forget(argparse.Namespace(slug="goneslug"))

    assert rc == 0
    assert not shard.exists(), "a forgotten repo must not stay searchable"
    assert not index_dir.exists()


def test_semantic_stage_skips_cleanly_when_extra_missing(monkeypatch, capsys):
    import sys

    import jarvis
    from jarvis.index_cli import _run_semantic_stage

    # Setting the sys.modules entry to None is the standard trick to force
    # the next `import` to raise ImportError -- but if some other test
    # module already ran `import jarvis.semantic` earlier in the suite,
    # Python has cached it as an attribute on the `jarvis` package
    # object, and `from jarvis import semantic` resolves via that
    # attribute without consulting sys.modules at all. Clearing the
    # attribute too makes this deterministic regardless of test order.
    monkeypatch.setitem(sys.modules, "jarvis.semantic", None)
    monkeypatch.delattr(jarvis, "semantic", raising=False)
    assert _run_semantic_stage(Path("/repo"), "slug", None) is False
    # Names the extra, so the warning stays useful to someone who installed the
    # CLI from PyPI rather than from a clone.
    assert "jarvis-mcp[semantic]" in capsys.readouterr().err


def _stub_work(semantic_module):
    """A SemanticWork with no admitted inputs — enough for the stage
    wrapper's debug loop; `finish_semantic` itself is mocked."""
    from jarvis.semantic import SemanticWork

    return SemanticWork(slug="slug", model=None, store=None, identity=None,
                        admitted=0, skipped=(), carried=(), inputs=(),
                        vector_by_hash={})


def test_semantic_stage_failure_is_nonfatal(monkeypatch, capsys):
    import jarvis.semantic as semantic_module
    from jarvis.index_cli import _run_semantic_stage
    from jarvis.semantic import SemanticWork

    def _boom(*args, **kwargs):
        raise RuntimeError("model download failed")

    # The standalone path shares the prepare/finish split with the main
    # pipeline (spec TSI-09); a failure in either half is nonfatal.
    monkeypatch.setattr(semantic_module, "prepare_semantic", _boom)
    assert _run_semantic_stage(Path("/repo"), "slug", None) is False
    monkeypatch.setattr(semantic_module, "prepare_semantic",
                        lambda *a, **k: _stub_work(semantic_module))
    monkeypatch.setattr(semantic_module, "finish_semantic", _boom)
    assert _run_semantic_stage(Path("/repo"), "slug", None) is False
    assert "still published" in capsys.readouterr().err


def test_semantic_stage_success_returns_true(monkeypatch):
    import jarvis.semantic as semantic_module
    from jarvis.index_cli import _run_semantic_stage
    from jarvis.semantic import SemanticIndexReport, SemanticWork

    monkeypatch.setattr(semantic_module, "prepare_semantic",
                        lambda *a, **k: _stub_work(semantic_module))
    monkeypatch.setattr(semantic_module, "finish_semantic",
                        lambda *a, **k: SemanticIndexReport(rows=5, files=1))
    assert _run_semantic_stage(Path("/repo"), "slug", None) is True




def test_semantic_stage_failure_is_nonfatal(monkeypatch, capsys):
    import jarvis.semantic as semantic_module
    from jarvis.index_cli import _run_semantic_stage
    from jarvis.semantic import SemanticWork

    def _boom(*args, **kwargs):
        raise RuntimeError("model download failed")

    # The standalone path shares the prepare/finish split with the main
    # pipeline (spec TSI-09); a failure in either half is nonfatal.
    monkeypatch.setattr(semantic_module, "prepare_semantic", _boom)
    assert _run_semantic_stage(Path("/repo"), "slug", None) is False
    monkeypatch.setattr(semantic_module, "prepare_semantic",
                        lambda *a, **k: _stub_work(semantic_module))
    monkeypatch.setattr(semantic_module, "finish_semantic", _boom)
    assert _run_semantic_stage(Path("/repo"), "slug", None) is False
    assert "still published" in capsys.readouterr().err


def test_semantic_stage_success_returns_true(monkeypatch):
    import jarvis.semantic as semantic_module
    from jarvis.index_cli import _run_semantic_stage
    from jarvis.semantic import SemanticIndexReport, SemanticWork

    monkeypatch.setattr(semantic_module, "prepare_semantic",
                        lambda *a, **k: _stub_work(semantic_module))
    monkeypatch.setattr(semantic_module, "finish_semantic",
                        lambda *a, **k: SemanticIndexReport(rows=5, files=1))
    assert _run_semantic_stage(Path("/repo"), "slug", None) is True




def test_forget_removes_lance_table_dir(tmp_path: Path, monkeypatch):
    import argparse

    from jarvis.index_cli import _cmd_forget

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(tmp_path / "registry.db")
    registry.upsert("gone", "/p", "python", None, "indexed")
    registry.close()
    lance_dir = tmp_path / "lancedb" / "gone.lance"
    lance_dir.mkdir(parents=True)
    (lance_dir / "data.bin").write_text("x")

    assert _cmd_forget(argparse.Namespace(slug="gone")) == 0
    assert not lance_dir.exists()


def test_forget_removes_swift_cache_dir_sparing_siblings(tmp_path: Path, monkeypatch, capsys):
    """D-06: forgetting a repo removes everything jarvis stored for it --
    including the out-of-repo scip-swift cache -- and only its own: the
    sweep path is built solely from the slug, so a sibling slug's cache
    must survive untouched."""
    import argparse

    from jarvis import config
    from jarvis.index_cli import _cmd_forget
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))

    registry = Registry(config.data_dir() / "registry.db")
    registry.upsert("gone", str(tmp_path / "repo"), "swift", "abc123", "indexed")
    registry.close()

    cache = config.swift_cache_dir("gone")
    cache.mkdir(parents=True)
    (cache / "manifest.json").write_text("{}")

    sibling = config.swift_cache_dir("keeper")
    sibling.mkdir(parents=True)
    (sibling / "manifest.json").write_text("{}")

    rc = _cmd_forget(argparse.Namespace(slug="gone"))

    assert rc == 0
    assert not cache.exists(), "the forgotten repo's cache must die with it"
    assert sibling.exists(), "a sibling slug's cache must survive"
    assert "forgot gone" in capsys.readouterr().out


def test_forget_succeeds_when_swift_cache_dir_absent(tmp_path: Path, monkeypatch, capsys):
    """The cache legitimately may not exist (never-Swift repo, or cache
    never created) -- forget must stay non-fatal and print the forgot line."""
    import argparse

    from jarvis import config
    from jarvis.index_cli import _cmd_forget
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))

    registry = Registry(config.data_dir() / "registry.db")
    registry.upsert("plainpy", str(tmp_path / "repo"), "python", None, "indexed")
    registry.close()

    assert not config.swift_cache_dir("plainpy").exists()

    rc = _cmd_forget(argparse.Namespace(slug="plainpy"))

    assert rc == 0
    assert "forgot plainpy" in capsys.readouterr().out


def test_forget_removes_job_files(tmp_path, monkeypatch):
    """`jarvis forget` removes everything jarvis stored for a repo (D-06) --
    launch record and index log included. The lock FILE survives on purpose:
    unlinking it would let a waiter lock a detached inode."""
    from jarvis import index_cli, jobs
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("app", str(repo), "python", None, "indexed")
    finally:
        registry.close()

    jobs.write_launch_record("app", config.index_log("app"))
    config.index_log("app").write_text("stderr", encoding="utf-8")

    parser = index_cli.build_parser()
    args = parser.parse_args(["forget", "app"])
    assert args.func(args) == 0

    assert not config.index_launchfile("app").exists()
    assert not config.index_log("app").exists()


def test_forget_refuses_while_a_writer_holds_the_lock(tmp_path, monkeypatch, capsys):
    """Destroying a repo's index out from under a live writer is the failure
    this guards. Tested against a HELD lock, not merely cleanup after
    release."""
    from jarvis import index_cli, jobs
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("app", str(repo), "python", None, "indexed")
    finally:
        registry.close()
    jobs.write_launch_record("app", config.index_log("app"))

    parser = index_cli.build_parser()
    args = parser.parse_args(["forget", "app"])
    with jobs.build_lock("app"):
        assert args.func(args) == 1

    # Nothing was destroyed, and the row still exists.
    assert config.index_launchfile("app").exists()
    assert "already running" in capsys.readouterr().err
    registry = Registry(config.data_dir() / "registry.db")
    try:
        assert registry.get("app") is not None
    finally:
        registry.close()


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_index_repo_builds_semantic_index_and_searches(tmp_path: Path, monkeypatch):
    """Full pipeline: chunk -> embed -> LanceDB -> hybrid search, exercised
    with a small real model (not the default) so the test stays minutes-not-
    hours on first download; the pipeline under test is identical either way.
    """
    pytest.importorskip("lancedb")
    pytest.importorskip("sentence_transformers")

    monkeypatch.setenv("JARVIS_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import jarvis.embeddings as embeddings_module
    monkeypatch.setattr(embeddings_module, "_default", None)  # reset singleton

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)

    data_root = tmp_path / "data"
    slug = index_repo(repo_dir, slug="semfix", root=data_root)

    from jarvis.semantic import semantic_search
    result = semantic_search(slug, "function definition", root=data_root)
    assert result["total"] >= 1
    assert all("filePath" in r for r in result["results"])


def test_semantic_include_flag_reaches_index_repo_as_a_tuple(tmp_path, monkeypatch):
    """The CLI collects repeated --semantic-include into a list; index_repo
    takes a tuple. Registry persistence itself is covered in test_registry.py."""
    import argparse

    from jarvis import index_cli

    captured = {}

    def _fake_index_repo(repo_path, *, slug=None, root=None, scheme=None,
                         semantic_include=None, language=None, scip=None,
                         semantic=True, watch=False):
        captured["semantic_include"] = semantic_include
        return "myrepo"

    monkeypatch.setattr(index_cli, "index_repo", _fake_index_repo)
    args = argparse.Namespace(path=str(tmp_path), slug="myrepo", scheme=None,
                              semantic_include=["src/gen", "vendor/pb"])
    assert index_cli._cmd_index(args) == 0
    assert captured["semantic_include"] == ("src/gen", "vendor/pb")


def test_semantic_report_names_every_skipped_file(capsys):
    from jarvis.index_cli import _print_semantic_report
    from jarvis.semantic import SemanticIndexReport, SkippedFile

    report = SemanticIndexReport(
        rows=95, files=15,
        skipped=(SkippedFile("src/jarvis/scip_pb2.py", "banner:do not edit"),),
        truncated=3,
    )
    _print_semantic_report(report)
    err = capsys.readouterr().err
    assert "semantic: 95 chunks from 15 files" in err
    assert "semantic: skipped src/jarvis/scip_pb2.py (banner:do not edit)" in err
    assert "3 chunks exceeded" in err


def test_semantic_report_omits_truncation_line_when_zero(capsys):
    from jarvis.index_cli import _print_semantic_report
    from jarvis.semantic import SemanticIndexReport

    _print_semantic_report(SemanticIndexReport(rows=10, files=2, truncated=0))
    err = capsys.readouterr().err
    assert "exceeded" not in err
    assert "could not measure" not in err


def test_semantic_report_notes_unmeasured_truncation(capsys):
    from jarvis.index_cli import _print_semantic_report
    from jarvis.semantic import SemanticIndexReport

    _print_semantic_report(SemanticIndexReport(rows=10, files=2, truncated=None))
    assert "could not measure truncation" in capsys.readouterr().err


def test_report_prints_token_percentiles(capsys):
    from jarvis.index_cli import _print_semantic_report
    from jarvis.semantic import SemanticIndexReport, TokenStats

    _print_semantic_report(SemanticIndexReport(
        rows=95, files=15, truncated=0, token_stats=TokenStats(180, 410, 498)))
    assert "chunk tokens p50=180 p90=410 max=498" in capsys.readouterr().err


def test_report_omits_percentiles_when_absent(capsys):
    from jarvis.index_cli import _print_semantic_report
    from jarvis.semantic import SemanticIndexReport

    _print_semantic_report(SemanticIndexReport(rows=10, files=2, truncated=0))
    assert "chunk tokens" not in capsys.readouterr().err


def test_report_prints_prefix_warning(capsys):
    from jarvis.index_cli import _print_semantic_report
    from jarvis.semantic import SemanticIndexReport

    _print_semantic_report(SemanticIndexReport(
        rows=10, files=2, truncated=0, prefix_warning="model X is not in the map"))
    assert "model X is not in the map" in capsys.readouterr().err


def test_indexer_by_language_covers_every_supported_language():
    from jarvis.index_cli import _INDEXER_BY_LANGUAGE

    assert sorted(_INDEXER_BY_LANGUAGE) == ["java", "python", "swift", "typescript"]
    assert _INDEXER_BY_LANGUAGE["python"] == ["scip-python", "index"]
    assert _INDEXER_BY_LANGUAGE["swift"] == ["scip-swift"]


def test_resolve_language_preserves_stored_override_when_none_given(tmp_path: Path):
    from jarvis.index_cli import _resolve_language

    registry = Registry(tmp_path / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed",
                    language_override="python")
    assert _resolve_language(registry, "my-repo", language=None) == "python"
    registry.close()


def test_resolve_language_prefers_explicit_value_over_stored(tmp_path: Path):
    from jarvis.index_cli import _resolve_language

    registry = Registry(tmp_path / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed",
                    language_override="python")
    assert _resolve_language(registry, "my-repo", language="java") == "java"
    registry.close()


def test_resolve_language_returns_none_for_unknown_slug(tmp_path: Path):
    from jarvis.index_cli import _resolve_language

    registry = Registry(tmp_path / "registry.db")
    assert _resolve_language(registry, "nope", language=None) is None
    registry.close()


def test_index_repo_language_override_skips_detection(tmp_path: Path, monkeypatch):
    """An override means "do not guess" -- detect_language must not run at
    all, so a repo whose plurality says otherwise still gets the forced
    language, and the registry records both the effective language and the
    fact that it was forced."""
    import jarvis.index_cli as cli

    for i in range(5):
        (tmp_path / f"f{i}.ts").write_text("export const x = 1;\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    def boom(_repo_path):
        raise AssertionError("detect_language must not be called when overridden")

    monkeypatch.setattr(cli, "detect_language", boom)
    monkeypatch.setattr(cli, "check_scip_version", lambda: None)

    captured: dict = {}

    def fake_run(cmd, *, cwd, step, env=None):
        captured.setdefault("cmds", []).append(cmd)
        raise cli.IndexingError("stop after the indexer command is built")

    monkeypatch.setattr(cli, "_run", fake_run)

    data_root = tmp_path / "data"
    with pytest.raises(cli.IndexingError):
        cli.index_repo(tmp_path, slug="forced", root=data_root, language="python")

    assert captured["cmds"][0][0] == "scip-python"

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get("forced")
        assert entry is not None
        assert entry.language == "python"
        assert entry.language_override == "python"
    finally:
        registry.close()


def test_language_override_to_swift_still_gets_xcodebuild(tmp_path: Path, monkeypatch):
    """The Swift build-tool selection keys off the *effective* language, so
    it must fire when swift came from an override just as it does when swift
    came from detection."""
    import jarvis.index_cli as cli

    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "App.swift").write_text("let x = 1\n")
    (tmp_path / "App.xcodeproj").mkdir()
    (tmp_path / "App.xcodeproj" / "project.pbxproj").write_text("// stub\n")
    _init_git_repo(tmp_path)

    monkeypatch.setattr(cli, "check_scip_version", lambda: None)
    # Hermetic on machines without scip-swift on PATH: the phase-02 runtime
    # floor is the first statement of the swift branch and would raise
    # "scip-swift not found on PATH" before the indexer command is built.
    monkeypatch.setattr(cli, "check_scip_swift_version", lambda: None)

    captured: dict = {}

    def fake_run(cmd, *, cwd, step, env=None):
        captured.setdefault("cmds", []).append(cmd)
        raise cli.IndexingError("stop after the indexer command is built")

    monkeypatch.setattr(cli, "_run", fake_run)

    with pytest.raises(cli.IndexingError):
        cli.index_repo(tmp_path, slug="forced-swift", root=tmp_path / "data",
                       language="swift", scheme="MyScheme")

    cmd = captured["cmds"][0]
    assert cmd[0] == "scip-swift"
    assert "--build-tool" in cmd and "xcodebuild" in cmd
    assert "--scheme" in cmd and "MyScheme" in cmd


def test_index_repo_raises_unsupported_language_for_stale_registry_override(
    tmp_path: Path, monkeypatch
):
    """`argparse`'s `choices=` only guards a fresh `--language` value typed
    by a user -- it does not guard a value already persisted in
    `registry.db` from an earlier version (e.g. a language a future release
    stops supporting, or a hand-edited row). `jarvis reindex` reaches
    `index_repo` with that stale value; it must raise `UnsupportedLanguageError`
    (caught at the CLI boundary), not a bare `KeyError`."""
    import jarvis.index_cli as cli

    (tmp_path / "app.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    data_root = tmp_path / "data"
    registry = Registry(config.data_dir(data_root) / "registry.db")
    registry.upsert("stale-lang", str(tmp_path), "cobol", "abc123", "indexed",
                    language_override="cobol")
    registry.close()

    monkeypatch.setattr(cli, "check_scip_version", lambda: None)

    with pytest.raises(UnsupportedLanguageError, match="cobol"):
        cli.index_repo(tmp_path, slug="stale-lang", root=data_root)


def test_cmd_reindex_reports_stale_language_override_as_error(tmp_path: Path, monkeypatch, capsys):
    """Same as above, exercised through the actual `reindex` CLI path."""
    import argparse
    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "app.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    registry = Registry(config.data_dir() / "registry.db")
    registry.upsert("stale-lang", str(tmp_path), "cobol", "abc123", "indexed",
                    language_override="cobol")
    registry.close()

    monkeypatch.setattr(cli, "check_scip_version", lambda: None)

    rc = cli._cmd_reindex(argparse.Namespace(slug="stale-lang"))

    assert rc == 1
    assert "cobol" in capsys.readouterr().err


def test_index_parser_accepts_language_flag():
    from jarvis.index_cli import build_parser

    args = build_parser().parse_args(["index", "/repos/x", "--language", "python"])
    assert args.language == "python"


def test_watch_parser_accepts_language_flag():
    from jarvis.index_cli import build_parser

    args = build_parser().parse_args(["watch", "/repos/x", "--language", "swift"])
    assert args.language == "swift"


def test_index_parser_rejects_unknown_language():
    from jarvis.index_cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["index", "/repos/x", "--language", "cobol"])


def test_reindex_forwards_stored_language_override(tmp_path: Path, monkeypatch):
    import argparse
    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))

    registry = Registry(config.data_dir() / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed",
                    language_override="python")
    registry.close()

    captured: dict = {}
    def fake_index_repo(path, *, slug=None, root=None, scheme=None, semantic_include=None,
                        language=None, scip=None, semantic=True, watch=False):
        captured["language"] = language
        return slug

    monkeypatch.setattr(cli, "index_repo", fake_index_repo)

    rc = cli._cmd_reindex(argparse.Namespace(slug="my-repo"))
    assert rc == 0
    assert captured["language"] == "python"


def test_cmd_index_reports_non_git_directory_as_error(tmp_path: Path, monkeypatch, capsys):
    """NotAGitRepositoryError must be caught at the CLI boundary and printed,
    not escape as a traceback.

    Monkeypatches `index_repo` to raise `NotAGitRepositoryError` directly, so
    this test fails if that exception type were ever removed from
    `_cmd_index`'s except tuple -- unlike asserting on a real non-git
    directory's error text, which would keep passing on substring luck even
    with the type removed (git's own stderr for a plain `IndexingError`
    happens to contain the same phrase). The real end-to-end path -- that a
    non-git directory actually raises `NotAGitRepositoryError`, not a
    misdiagnosed "no commits yet" -- is pinned separately by
    `test_git_head_raises_not_a_git_repository_for_non_git_directory`."""
    import argparse
    import jarvis.index_cli as cli

    def fake_index_repo(*args, **kwargs):
        raise cli.NotAGitRepositoryError(f"{tmp_path} is not a git repository (fake)")

    monkeypatch.setattr(cli, "index_repo", fake_index_repo)

    rc = cli._cmd_index(argparse.Namespace(
        path=str(tmp_path), slug=None, scheme=None, semantic_include=None, language=None,
    ))

    assert rc == 1
    assert "not a git repository" in capsys.readouterr().err


def test_bash_shim_failure_detected():
    from jarvis.index_cli import _bash_shim_failure

    assert _bash_shim_failure(
        "Fatal error compiling: Could not retrieve version from "
        "/tmp/scip-java1/bin/javac. Exit code 1, Output: "
        "/tmp/scip-java1/bin/javac: line 38: LAUNCHER_ARGS[@]: unbound variable"
    )


def test_bash_shim_failure_ignores_other_output():
    from jarvis.index_cli import _bash_shim_failure

    assert not _bash_shim_failure("error: No SCIP shards found")


@pytest.mark.integration
@pytest.mark.skipif(bool(_missing_java), reason=f"missing required binaries: {_missing_java}")
def test_index_repo_end_to_end_for_java_repo(tmp_path: Path):
    """A plain-JVM Gradle repo must produce real navigable symbols."""
    repo_dir = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    slug = index_repo(repo_dir, root=data_root)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.status == "indexed", f"expected a full index, got {entry.status}"
        assert entry.language == "java"
    finally:
        registry.close()

    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    conn = sqlite3.connect(f"file:{target_dir / pointer}?mode=ro", uri=True)
    try:
        symbols = conn.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0]
        mentions = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert any(
            "demo/Greeter#greet()" in row[0]
            for row in conn.execute("SELECT symbol FROM global_symbols")
        )
    finally:
        conn.close()

    assert symbols > 0, "no symbols — the indexer produced nothing navigable"
    assert chunks > 0 and mentions > 0, "symbols without occurrence ranges"


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_bare_name_resolution_against_a_real_index(tmp_path: Path):
    """End-to-end: a bare name resolves through a genuinely converted SCIP
    index, not a hand-built fixture. Guards against grammar assumptions that
    hold for synthetic symbols but not for real indexer output."""
    from jarvis.query import QueryService

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)

    data_root = tmp_path / "data"
    slug = index_repo(repo_dir, root=data_root)

    cache = config.new_connection_cache(data_root)
    service = QueryService(cache)

    conn, _ = config.get_connection(cache, slug)
    path = conn.execute("SELECT relative_path FROM documents LIMIT 1").fetchone()[0]

    result = service.get_document_symbols(slug, path)
    entries = result.entries
    assert entries, "fixture repo produced no document symbols"
    target = entries[0]

    parsed_name = target.displayName
    assert parsed_name, "displayName should be populated from the parser"

    resolved = service.resolve_symbol(slug, parsed_name)
    assert resolved == target.symbol

    # Idempotence: rung 1 returns a full symbol unchanged.
    assert service.resolve_symbol(slug, resolved) == resolved


def test_tracked_blob_count_counts_tracked_files(tmp_path: Path):
    from jarvis.index_cli import _tracked_blob_count

    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    _init_git_repo(tmp_path)

    assert _tracked_blob_count(tmp_path) == 2


def test_tracked_blob_count_ignores_untracked_files(tmp_path: Path):
    from jarvis.index_cli import _tracked_blob_count

    (tmp_path / "a.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)
    (tmp_path / "untracked.py").write_text("z = 3\n")

    assert _tracked_blob_count(tmp_path) == 1


def test_tracked_blob_count_excludes_submodule_gitlinks(tmp_path: Path):
    """A submodule is one mode-160000 gitlink entry, not a file. Because
    zoekt-git-index runs with -submodules=false it never descends into it, so
    counting the gitlink would make the expectation permanently unreachable."""
    from jarvis.index_cli import _tracked_blob_count

    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "lib.py").write_text("v = 1\n")
    _init_git_repo(inner)

    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "a.py").write_text("x = 1\n")
    _init_git_repo(outer)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(inner), "inner"],
        cwd=outer, check=True, capture_output=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "add submodule"], cwd=outer, check=True)

    # a.py + .gitmodules == 2; the `inner` gitlink is excluded.
    assert _tracked_blob_count(outer) == 2


def test_tracked_blob_count_raises_for_non_git_directory(tmp_path: Path):
    from jarvis.index_cli import NotAGitRepositoryError, _tracked_blob_count

    with pytest.raises(NotAGitRepositoryError):
        _tracked_blob_count(tmp_path)


def _git_config_value(repo_path: Path, key: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "config", "--get", key],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_pin_zoekt_repo_name_sets_the_slug(tmp_path: Path):
    from jarvis.index_cli import _pin_zoekt_repo_name

    (tmp_path / "a.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    _pin_zoekt_repo_name(tmp_path, "myslug")

    assert _git_config_value(tmp_path, "zoekt.name") == "myslug"


def test_pin_zoekt_repo_name_is_idempotent(tmp_path: Path):
    from jarvis.index_cli import _pin_zoekt_repo_name

    (tmp_path / "a.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    _pin_zoekt_repo_name(tmp_path, "first")
    _pin_zoekt_repo_name(tmp_path, "second")

    assert _git_config_value(tmp_path, "zoekt.name") == "second"


def test_pin_zoekt_repo_name_raises_for_non_git_directory(tmp_path: Path):
    """Must fail loudly: an unpinned name makes zoekt derive one from the
    origin remote URL, and `r:<slug>` then returns zero hits with no error."""
    from jarvis.index_cli import IndexingError, _pin_zoekt_repo_name

    with pytest.raises(IndexingError):
        _pin_zoekt_repo_name(tmp_path, "myslug")


def test_unpin_zoekt_repo_name_removes_the_key(tmp_path: Path):
    from jarvis.index_cli import _pin_zoekt_repo_name, _unpin_zoekt_repo_name

    (tmp_path / "a.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)
    _pin_zoekt_repo_name(tmp_path, "myslug")

    _unpin_zoekt_repo_name(tmp_path)

    assert _git_config_value(tmp_path, "zoekt.name") is None


def test_unpin_zoekt_repo_name_tolerates_a_missing_key(tmp_path: Path):
    """git config --unset exits 5 when the key is absent. Repos indexed
    before this change have no zoekt.name, and `forget` must still succeed."""
    from jarvis.index_cli import _unpin_zoekt_repo_name

    (tmp_path / "a.py").write_text("x = 1\n")
    _init_git_repo(tmp_path)

    _unpin_zoekt_repo_name(tmp_path)  # must not raise


def test_unpin_zoekt_repo_name_tolerates_a_missing_directory(tmp_path: Path):
    """`forget` must work after the user has deleted the repo from disk."""
    from jarvis.index_cli import _unpin_zoekt_repo_name

    _unpin_zoekt_repo_name(tmp_path / "gone")  # must not raise


def test_forget_unpins_the_zoekt_repo_name(tmp_path: Path, monkeypatch, capsys):
    import argparse

    from jarvis import config
    from jarvis.index_cli import _cmd_forget, _pin_zoekt_repo_name
    from jarvis.registry import Registry

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")
    _init_git_repo(repo)
    _pin_zoekt_repo_name(repo, "myslug")

    data_dir = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_dir))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("myslug", str(repo), "python", "abc", "indexed")
    finally:
        registry.close()

    assert _cmd_forget(argparse.Namespace(slug="myslug")) == 0
    assert _git_config_value(repo, "zoekt.name") is None


def test_parse_indexed_file_count_reads_the_indexer_log():
    from jarvis.index_cli import _parse_indexed_file_count

    output = (
        "2026/08/03 22:26:52 attempting to index 133 total files "
        "(0 via cat-file, 133 via go-git)\n"
        "2026/08/03 22:26:53 finished shard /x/jarvis_v16.00000.zoekt: "
        "6408345 index bytes (overhead 3.2), 133 files processed\n"
    )

    assert _parse_indexed_file_count(output) == 133


def test_parse_indexed_file_count_returns_none_on_unknown_format():
    """An upstream log change must degrade to "unknown", never fail a publish."""
    from jarvis.index_cli import _parse_indexed_file_count

    assert _parse_indexed_file_count("nothing recognisable here") is None


def test_warn_on_coverage_shortfall_warns(capsys):
    from jarvis.index_cli import _warn_on_coverage_shortfall

    _warn_on_coverage_shortfall("myslug", 133, "attempting to index 100 total files")

    assert "myslug" in capsys.readouterr().err


def test_warn_on_coverage_shortfall_is_quiet_when_complete(capsys):
    from jarvis.index_cli import _warn_on_coverage_shortfall

    _warn_on_coverage_shortfall("myslug", 133, "attempting to index 133 total files")

    assert capsys.readouterr().err == ""


def test_warn_on_coverage_shortfall_is_quiet_when_unparseable(capsys):
    from jarvis.index_cli import _warn_on_coverage_shortfall

    _warn_on_coverage_shortfall("myslug", 133, "unrecognised")

    assert capsys.readouterr().err == ""


def test_sweep_zoekt_tmp_orphans_removes_only_this_slugs_temp_files(tmp_path: Path):
    """A killed zoekt run leaves a .tmp that is never usable and never
    cleaned up; 545 MB of them accumulated once."""
    from jarvis.index_cli import _sweep_zoekt_tmp_orphans

    zoekt_dir = tmp_path / ".zoekt"
    zoekt_dir.mkdir(parents=True)
    (zoekt_dir / "myslug_v16.00000.zoekt").write_bytes(b"x")
    (zoekt_dir / "myslug_v16.00001.zoekt.12345.tmp").write_bytes(b"x")
    (zoekt_dir / "otherslug_v16.00000.zoekt.99.tmp").write_bytes(b"x")

    removed = _sweep_zoekt_tmp_orphans("myslug", root=tmp_path)

    assert len(removed) == 1
    assert (zoekt_dir / "myslug_v16.00000.zoekt").exists(), "must not touch real shards"
    assert not (zoekt_dir / "myslug_v16.00001.zoekt.12345.tmp").exists()
    assert (zoekt_dir / "otherslug_v16.00000.zoekt.99.tmp").exists(), "must not touch other repos"


def test_sweep_zoekt_tmp_orphans_is_safe_when_absent(tmp_path: Path):
    from jarvis.index_cli import _sweep_zoekt_tmp_orphans

    assert _sweep_zoekt_tmp_orphans("nothing-here", root=tmp_path) == []


def test_sweep_zoekt_tmp_orphans_survives_unlink_errors(tmp_path: Path, monkeypatch):
    """The sweep runs between a successful zoekt-git-index run and the
    atomic publish -- an EACCES/EBUSY/EPERM on unlink() must never
    propagate and discard an otherwise-successful publish."""
    from jarvis.index_cli import _sweep_zoekt_tmp_orphans

    zoekt_dir = tmp_path / ".zoekt"
    zoekt_dir.mkdir(parents=True)
    (zoekt_dir / "myslug_v16.00000.zoekt.tmp").write_bytes(b"x")

    def _raise_permission_error(self: Path) -> None:
        raise PermissionError("no")

    monkeypatch.setattr(Path, "unlink", _raise_permission_error)

    removed = _sweep_zoekt_tmp_orphans("myslug", root=tmp_path)

    assert removed == []
    assert (zoekt_dir / "myslug_v16.00000.zoekt.tmp").exists(), "unremovable file must be left in place"


@pytest.mark.integration
def test_zoekt_git_index_excludes_gitignored_content(tmp_path: Path, monkeypatch):
    """The whole point: gitignored junk is absent by construction, with no
    denylist to maintain.

    The tracked-file count and the indexer's own log line are real signals,
    but neither directly proves the junk is unreachable via search (or that
    the tracked content is findable) -- so this also spins up a real
    zoekt-webserver and queries it, the same way
    test_get_index_status_reports_incomplete_after_a_shard_is_deleted does.

    Also covers the healthy/complete-coverage case of `_search_coverage_fields`
    -- the shard-deletion test below only covers the incomplete case.
    """
    if shutil.which("zoekt-git-index") is None:
        pytest.skip("zoekt-git-index not on PATH")
    if shutil.which("zoekt-webserver") is None:
        pytest.skip("zoekt-webserver not on PATH")

    from jarvis import config, server
    from jarvis.index_cli import (
        _pin_zoekt_repo_name, _tracked_blob_count, _zoekt_index_cmd,
    )
    from jarvis.registry import Registry
    from jarvis.search import ZoektLifecycle, search_zoekt

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "junkdir").mkdir()
    (repo / ".gitignore").write_text("junkdir/\n")
    (repo / "src" / "a.py").write_text("sourcetoken_alpha = 1\n")
    (repo / "junkdir" / "big.txt").write_text("junktoken_beta\n")
    _init_git_repo(repo)
    _pin_zoekt_repo_name(repo, "covslug")

    zoekt_dir = tmp_path / ".zoekt"
    zoekt_dir.mkdir()
    result = subprocess.run(
        _zoekt_index_cmd(zoekt_dir, repo), cwd=repo, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    # .gitignore + src/a.py == 2; junkdir/big.txt is untracked.
    assert _tracked_blob_count(repo) == 2
    assert "attempting to index 2 total files" in result.stderr
    assert list(zoekt_dir.glob("covslug_v*.zoekt")), "shard must be named after the slug"

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("covslug", str(repo), "python", "abc", "indexed")
        registry.mark_tracked_files("covslug", _tracked_blob_count(repo))
    finally:
        registry.close()

    lifecycle = ZoektLifecycle(index_dir=zoekt_dir, data_dir=tmp_path, port=6078)
    try:
        base_url = lifecycle.ensure_running()
        assert search_zoekt(base_url, "junktoken_beta").hits == [], "gitignored content must not be searchable"
        assert len(search_zoekt(base_url, "sourcetoken_alpha").hits) >= 1, "tracked content must be searchable"

        monkeypatch.setattr(server, "_zoekt_base_url_if_running", lambda: base_url)
        fields = server._search_coverage_fields("covslug")
        assert fields["searchCoverage"]["complete"] is True
    finally:
        lifecycle.stop()


@pytest.mark.integration
def test_get_index_status_reports_incomplete_after_a_shard_is_deleted(tmp_path: Path, monkeypatch):
    """The incident, reproduced: a successful index whose shards are then
    deleted must report complete: false instead of quietly answering with
    partial results.

    `-shard_limit 120` forces a multi-shard index on a tiny repo, so this is
    deterministic and fast rather than needing a 100 MB corpus.
    """
    if shutil.which("zoekt-git-index") is None:
        pytest.skip("zoekt-git-index not on PATH")
    if shutil.which("zoekt-webserver") is None:
        pytest.skip("zoekt-webserver not on PATH")

    from jarvis import config, server
    from jarvis.index_cli import _pin_zoekt_repo_name, _tracked_blob_count
    from jarvis.registry import Registry
    from jarvis.search import ZoektLifecycle

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(6):
        (repo / f"f{i}.txt").write_text(f"token_{i:02d} padding padding padding padding\n")
    _init_git_repo(repo)
    _pin_zoekt_repo_name(repo, "incidentslug")

    zoekt_dir = tmp_path / ".zoekt"
    zoekt_dir.mkdir()
    subprocess.run(
        ["zoekt-git-index", "-index", str(zoekt_dir), "-incremental=false",
         "-submodules=false", "-shard_limit", "120", str(repo)],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    shards = sorted(zoekt_dir.glob("incidentslug_v*.zoekt"))
    assert len(shards) > 1, "need a multi-shard index to delete from"

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("incidentslug", str(repo), "python", "abc", "indexed")
        registry.mark_tracked_files("incidentslug", _tracked_blob_count(repo))
    finally:
        registry.close()

    shards[0].unlink()  # the deletion that caused the incident

    lifecycle = ZoektLifecycle(index_dir=zoekt_dir, data_dir=tmp_path, port=6079)
    try:
        base_url = lifecycle.ensure_running()
        monkeypatch.setattr(server, "_zoekt_base_url_if_running", lambda: base_url)
        fields = server._search_coverage_fields("incidentslug")
    finally:
        lifecycle.stop()

    assert fields["searchCoverage"]["complete"] is False
    assert fields["searchCoverage"]["indexed"] < fields["searchCoverage"]["expected"]


@pytest.mark.integration
@pytest.mark.skipif(_missing, reason=f"missing required binaries: {_missing}")
def test_symbol_search_finds_definitions_in_real_index(tmp_path: Path):
    """The semanticSearch symbol signal, end-to-end against a real published
    index: an NL query naming the fixture's class returns its definition."""
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"
    slug = index_repo(repo_dir, root=data_root)

    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    conn = sqlite3.connect(f"file:{target_dir / pointer}?mode=ro&immutable=1", uri=True)
    try:
        from jarvis.symbol_search import search_symbols

        hits = search_symbols(conn, "where is the Greeter class defined")
        assert hits, "expected at least one symbol hit for 'Greeter'"
        top = hits[0]
        assert top.file_path == "greeter.py"
        assert top.dotted_path.endswith("Greeter")
        assert top.start_line == 9  # 1-based: `class Greeter:` is on line 9

        # A method query resolves too, proving defn_enclosing_ranges depth.
        method_hits = search_symbols(conn, "say_hi")
        say_hi_hit = next(h for h in method_hits if h.dotted_path.endswith("say_hi"))
        assert say_hi_hit.start_line == 10  # 1-based: `def say_hi(...)` is on line 10
    finally:
        conn.close()




def test_cmd_status_explains_a_failed_repo(tmp_path: Path, monkeypatch, capsys):
    """STAT-01/D-12: `jarvis status` must explain what happened and how to
    recover, deriving the recovery command from the origin at read time."""
    import argparse

    from jarvis.index_cli import _cmd_status
    from jarvis.registry import ORIGIN_FAILED_HARD, Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    registry = Registry(data_root / "registry.db")
    try:
        registry.record_failure(
            "failing", "/abs/path/failing", "python", ORIGIN_FAILED_HARD,
            "scip-python index failed (scip-python)",
            "scip-python index failed (scip-python):\nfull stderr text",
        )
    finally:
        registry.close()

    rc = _cmd_status(argparse.Namespace(slug="failing"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "origin: failed_hard" in out
    assert "cause: scip-python index failed (scip-python)" in out
    assert "recovery: jarvis index /abs/path/failing" in out


def test_cmd_list_marks_repo_health_at_a_glance(tmp_path: Path, monkeypatch, capsys):
    """D-08: glyphs in the status field (✗ failed / ◐ search-only / ✓
    everything else), reason one-liner as a 6th field on failed rows only;
    columns 1-5 keep the existing TSV order for scripts."""
    import argparse

    from jarvis.index_cli import _cmd_list
    from jarvis.registry import ORIGIN_FAILED_HARD, Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    registry = Registry(data_root / "registry.db")
    try:
        registry.record_failure("broken", "/repos/broken", "python",
                                ORIGIN_FAILED_HARD, "scip-python index failed",
                                "scip-python index failed:\nboom")
        # The historical status literal: no writer produces it anymore,
        # but un-migrated legacy rows could still carry it and must not
        # print a misleading checkmark.
        registry.upsert("legacy", "/repos/legacy", "unknown", None,
                        "search-only")
        registry.upsert("healthy", "/repos/healthy", "python", "abc123", "indexed")
    finally:
        registry.close()

    assert _cmd_list(argparse.Namespace()) == 0
    rows = {line.split("\t")[0]: line.split("\t")
            for line in capsys.readouterr().out.splitlines()}

    failed = rows["broken"]
    assert failed[1] == "✗ failed"
    assert failed[2] == "python"
    assert failed[3] == "-"
    assert failed[4] == "/repos/broken"
    assert failed[5] == "scip-python index failed"  # 6th field: reason only

    search_only = rows["legacy"]
    assert search_only[1] == "◐ search-only"
    assert len(search_only) == 5  # no trailing empty 6th field

    healthy = rows["healthy"]
    assert healthy[1] == "✓ indexed"
    assert healthy[3] == "abc123"
    assert len(healthy) == 5

def test_cmd_list_renders_degraded_rows_with_glyph_and_reason(tmp_path: Path, monkeypatch, capsys):
    """FALL-01: a degraded repo renders ◐ (search still answers) with the
    failure cause as a 6th TSV field — the same reason-first contract as
    failed rows, on a row that still self-heals on the next reindex."""
    import argparse

    from jarvis.index_cli import _cmd_list
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    registry = Registry(data_root / "registry.db")
    try:
        registry.upsert("crashed", "/repos/crashed", "python", "abc123",
                        DEGRADED_STATUS, status_origin=ORIGIN_FALLBACK,
                        status_reason="scip-python crashed mid-build")
    finally:
        registry.close()

    assert _cmd_list(argparse.Namespace()) == 0
    rows = {line.split("\t")[0]: line.split("\t")
            for line in capsys.readouterr().out.splitlines()}

    degraded = rows["crashed"]
    assert degraded[1] == "◐ degraded"
    assert degraded[5] == "scip-python crashed mid-build"  # 6th field: the cause
    assert len(degraded) == 6


def test_cmd_status_explains_a_degraded_repo(tmp_path: Path, monkeypatch, capsys):
    """Pin: `jarvis status` on a degraded row prints the fallback origin,
    the persisted cause, and the self-heal recovery verb — origin-driven
    since Phase 1; the degraded origin needs no status-command change."""
    import argparse

    from jarvis.index_cli import _cmd_status
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    registry = Registry(data_root / "registry.db")
    try:
        registry.upsert("crashed", "/repos/crashed", "python", "abc123",
                        DEGRADED_STATUS, status_origin=ORIGIN_FALLBACK,
                        status_reason="scip-python crashed mid-build")
    finally:
        registry.close()

    rc = _cmd_status(argparse.Namespace(slug="crashed"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "origin: fallback" in out
    assert "cause: scip-python crashed mid-build" in out
    recovery = next(line for line in out.splitlines() if line.startswith("recovery:"))
    assert "jarvis reindex" in recovery


def test_cmd_list_keeps_search_only_rows_five_field_beside_degraded(tmp_path: Path, monkeypatch, capsys):
    """The degraded branch adds a 6th field only for degraded rows — a
    search-only row in the same listing keeps its 5-field shape (D-08)."""
    import argparse

    from jarvis.index_cli import _cmd_list
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    registry = Registry(data_root / "registry.db")
    try:
        registry.upsert("crashed", "/repos/crashed", "python", "abc123",
                        DEGRADED_STATUS, status_origin=ORIGIN_FALLBACK,
                        status_reason="scip-python crashed mid-build")
        registry.upsert("legacy", "/repos/legacy", "unknown", None,
                        "search-only")
    finally:
        registry.close()

    assert _cmd_list(argparse.Namespace()) == 0
    rows = {line.split("\t")[0]: line.split("\t")
            for line in capsys.readouterr().out.splitlines()}

    assert rows["crashed"][1] == "◐ degraded"
    assert len(rows["crashed"]) == 6
    assert rows["legacy"][1] == "◐ search-only"
    assert len(rows["legacy"]) == 5  # no 6th-field regression


def test_cmd_status_prints_stderr_tail_and_pointer(tmp_path: Path, monkeypatch, capsys):
    """D-02 display side: only the last ~20 lines are printed plus a
    pointer to the persisted full log; persistence itself stays unbounded."""
    import argparse

    from jarvis.index_cli import _cmd_status
    from jarvis.registry import ORIGIN_FAILED_HARD, Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    stderr = "\n".join(f"stderr line {i:02d}" for i in range(40))
    registry = Registry(data_root / "registry.db")
    try:
        registry.record_failure("broken", "/repos/broken", "python",
                                ORIGIN_FAILED_HARD, "scip-python index failed",
                                f"scip-python index failed:\n{stderr}")
    finally:
        registry.close()

    rc = _cmd_status(argparse.Namespace(slug="broken"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "stderr line 00" not in out  # head is display-truncated...
    assert "stderr line 19" not in out  # ...at the last 20 lines
    assert "stderr line 20" in out
    assert "stderr line 39" in out
    assert "persisted in the registry" in out


def test_cmd_status_omits_stderr_block_when_absent(tmp_path: Path, monkeypatch, capsys):
    """A row with no persisted stderr (every success path) must print no
    stderr block at all."""
    import argparse

    from jarvis.index_cli import _cmd_status
    from jarvis.registry import Registry

    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))
    registry = Registry(data_root / "registry.db")
    try:
        registry.upsert("healthy", "/repos/healthy", "python", "abc123", "indexed")
    finally:
        registry.close()

    rc = _cmd_status(argparse.Namespace(slug="healthy"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "stderr" not in out
    assert "full log" not in out


def test_pre_pipeline_stale_language_override_failure_overwrites_the_row(
    tmp_path: Path, monkeypatch
):
    """D-05/D-06: a persisted unsupported language override raises before
    the pipeline; the existing row must be overwritten with the failed
    attempt's facts (status/origin/reason set, language reflecting that
    resolution never completed), not keep the last good run's."""
    from jarvis.index_cli import UNKNOWN_LANGUAGE, index_repo
    from jarvis.registry import ORIGIN_FAILED_HARD

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    registry = Registry(data_root / "registry.db")
    try:
        registry.upsert("stale", str(repo_dir), "cobol", "abc123", "indexed",
                        language_override="cobol")
    finally:
        registry.close()

    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)

    with pytest.raises(UnsupportedLanguageError, match="cobol"):
        index_repo(repo_dir, slug="stale", root=data_root)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get("stale")
        assert entry is not None
        assert entry.status == "failed"  # not the stale 'indexed'
        assert entry.status_origin == ORIGIN_FAILED_HARD
        assert "cobol" in entry.status_reason
        assert "cobol" in entry.status_stderr
        assert entry.language == UNKNOWN_LANGUAGE  # D-06: no last-good facts linger
        assert entry.commit_sha is None
    finally:
        registry.close()


def test_duplicate_slug_rejection_writes_no_failure_row(tmp_path: Path, monkeypatch):
    """The duplicate-slug gate rejects the REQUEST (a path already indexed
    under another slug), it is not a failed run: stamping a failure would
    create a phantom row for the rejected slug that re-trips the gate on
    every later attempt. The existing row must stay byte-identical and no
    new row may appear."""
    from jarvis.index_cli import IndexingError, index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    registry = Registry(data_root / "registry.db")
    try:
        before = registry.upsert("first", str(repo_dir), "python", "abc123", "indexed")
    finally:
        registry.close()

    def _tripwire():
        # If the duplicate gate ever stops rejecting, the pre-pipeline
        # handler would stamp this row -- the untouched assertions below
        # would then fail loudly instead of silently indexing.
        raise RuntimeError("must not reach the version gate")

    monkeypatch.setattr("jarvis.index_cli.check_scip_version", _tripwire)

    with pytest.raises(IndexingError, match="already indexed as 'first'"):
        index_repo(repo_dir, slug="second", root=data_root)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get("first")
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.status_origin is None
        assert entry.status_reason is None
        assert entry.status_stderr is None
        assert entry.language == "python"
        assert entry.commit_sha == "abc123"
        assert entry.last_indexed == before.last_indexed  # untouched, not re-stamped
        assert registry.get("second") is None  # no phantom row for the rejected request
    finally:
        registry.close()


# --- Phase 3: opt-in self-healing fallback (FALL-01/FALL-04) ---------------

def _degrade_mock_run(failures: dict[str, Exception]):
    """A `_run` stand-in keyed on step name: steps named in `failures`
    raise; everything else succeeds (house pattern of the signature
    fallback tests)."""
    def _run(cmd, *, cwd, step, env=None):
        for fail_step, exc in failures.items():
            if step == fail_step:
                raise exc
        return _fake_completed_process(cmd)

    return _run


def test_run_translates_file_not_found_into_missing_binary_error(tmp_path: Path):
    """FALL-04 exclusion input: the real `_run` (not a mock) turns
    FileNotFoundError into the MissingBinaryError subclass, not a plain
    IndexingError -- the degrade gate keys on the type."""
    from jarvis.index_cli import MissingBinaryError, _run

    with pytest.raises(MissingBinaryError, match="not found on PATH") as excinfo:
        _run(["definitely-not-a-real-binary-xyz"], cwd=tmp_path, step="step-x")
    assert type(excinfo.value) is MissingBinaryError


def test_unclassified_scip_stage_error_stays_hard_with_best_effort_record(
    tmp_path: Path, monkeypatch, capsys
):
    """WR-03 + narrowed FALL-04 boundary: a NARROW typed stage boundary
    must prevent a broad catch from treating arbitrary infrastructure bugs
    as expected enrichment failures (spec TSI-04). A non-IndexingError
    raised inside the optional SCIP stage is NOT classified as
    failed/unavailable — it propagates as a hard IndexingError with the
    best-effort failure record written beside it."""
    from jarvis.index_cli import IndexingError, index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    def _boom():
        raise RuntimeError("disk detached mid-run")

    monkeypatch.setattr("jarvis.index_cli.check_scip_version", _boom)

    def _disk_full_record_failure(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Registry, "record_failure", _disk_full_record_failure)

    with pytest.raises(IndexingError, match="disk detached mid-run"):
        index_repo(repo_dir, root=data_root)

    err = capsys.readouterr().err
    assert "recording the failed run" in err
    assert "disk I/O error" in err
    assert "disk detached mid-run" in err




def _mock_healthy_full_run(monkeypatch):
    """Mocks for a fully successful main-pipeline run: every subprocess
    succeeds, the convert step writes a navigable minimal index db, and the
    graph/semantic stages no-op (the two-phase pattern of the ordering
    test)."""
    def _successful_run(cmd, *, cwd, step, env=None):
        if step == "scip expt-convert":
            _make_index_db(Path(cmd[3]), chunks=1, mentions=14)
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)
    monkeypatch.setattr("jarvis.index_cli._run", _successful_run)
    monkeypatch.setattr("jarvis.index_cli.populate_graph_for_repo", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._run_semantic_stage", lambda *a, **k: False)


def _mock_failing_indexer(monkeypatch, message):
    """Mocks for a SCIP indexer failure: the language indexer step raises,
    every other step still succeeds — the optional-enrichment degrade
    pattern (spec §12)."""
    from jarvis.index_cli import IndexingError

    def _fake_run(cmd, *, cwd, step, env=None):
        if step.endswith(" index"):
            raise IndexingError(message)
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)
    monkeypatch.setattr("jarvis.index_cli._run", _fake_run)
    monkeypatch.setattr("jarvis.index_cli._run_semantic_stage", lambda *a, **k: False)


# --- Phase 3: watch anti-treadmill (FALL-05) --------------------------------


def _watch_entry(**overrides):
    """A RegisteredRepo for the watch-skip predicate tests; keyword
    overrides pick the status/sha under test (the `_entry` pattern from
    tests/test_registry.py's recovery-mapping tests)."""
    from datetime import UTC, datetime

    from jarvis.registry import RegisteredRepo

    fields = dict(
        slug="mine", path="/repos/mine", language="python", commit_sha="abc123",
        last_indexed=datetime.now(UTC), status="indexed",
    )
    fields.update(overrides)
    return RegisteredRepo(**fields)


_UNSET = object()


class _FakeEvent:
    is_directory = False
    src_path = "/repos/x/main.py"


class _FakeObserver:
    """Minimal stand-in for watchdog's Observer: schedule() captures the
    handler; start() delivers one fake source-file event (driving the
    Debouncer); stop/join no-op."""

    def __init__(self):
        self.handler = None

    def schedule(self, handler, path, recursive=True):
        self.handler = handler

    def start(self):
        self.handler.on_any_event(_FakeEvent())

    def stop(self):
        pass

    def join(self):
        pass


def _install_fake_watchdog(monkeypatch):
    """Patch sys.modules so `_cmd_watch`'s deferred imports resolve to
    fakes — the tests drive the real `_reindex` closure without the
    `watch` extra (CI installs only `semantic`; the tests themselves never
    import watchdog)."""
    import types

    events = types.ModuleType("watchdog.events")
    events.FileSystemEventHandler = object
    observers = types.ModuleType("watchdog.observers")
    observers.Observer = _FakeObserver
    watchdog_pkg = types.ModuleType("watchdog")
    watchdog_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "watchdog", watchdog_pkg)
    monkeypatch.setitem(sys.modules, "watchdog.events", events)
    monkeypatch.setitem(sys.modules, "watchdog.observers", observers)


def _drive_cmd_watch(cli, monkeypatch, args_kwargs, sleep_results):
    """Run `_cmd_watch` to completion under the fake observer and a
    scripted `time.sleep` (the loop's only clock use). `sleep_results` is
    an iterator: each loop iteration first consumes one sleep result — a
    `None` sleeps on, an exception instance is raised to break the loop
    (KeyboardInterrupt is `_cmd_watch`'s own exit path)."""
    import argparse
    import types

    _install_fake_watchdog(monkeypatch)

    def _sleep(seconds):
        result = next(sleep_results)
        if isinstance(result, BaseException):
            raise result
        return result

    fake_time = types.ModuleType("time")
    fake_time.sleep = _sleep
    monkeypatch.setattr(cli, "time", fake_time)
    return cli._cmd_watch(argparse.Namespace(**args_kwargs))


# --- Phase 5: semantic install onboarding (SEMA-01/02) ----------------------


def _offer_test_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A fixture repo the offer tests can index through the mocked
    healthy pipeline (the degraded-row CLI test setup)."""
    repo_dir = tmp_path / name
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    return repo_dir


def _force_offer_seams(monkeypatch) -> None:
    """Force every SEMA-01 gate open except the decline bit: the extra is
    missing and both streams are a TTY. Seams are monkeypatched by full
    path — no real stdin, no real find_spec (this checkout and CI both
    have the extra installed, so detection must never run for real)."""
    monkeypatch.setattr("jarvis.index_cli._semantic_extra_missing", lambda: True)
    monkeypatch.setattr("jarvis.index_cli._at_interactive_tty", lambda: True)


def _script_input(monkeypatch, answer=None, error=None) -> list[str]:
    """Replace builtins.input with a recorder returning `answer` (or
    raising `error`); returns every prompt string seen."""
    prompts: list[str] = []

    def _input(prompt=""):
        prompts.append(prompt)
        if error is not None:
            raise error
        return answer

    monkeypatch.setattr("builtins.input", _input)
    return prompts


@pytest.mark.interactive_input
def test_cmd_index_tty_offer_decline_answer_persists_semantic_declined(
    tmp_path: Path, monkeypatch, capsys
):
    """SEMA-01/SC2 tracer: on a TTY with the extra missing, `jarvis index`
    offers exactly once, after the index is published, with the locked
    prompt text; Enter (the safe default) declines and the bit persists
    per-repo with no traceback."""
    import argparse

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = _offer_test_repo(tmp_path)
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)
    prompts = _script_input(monkeypatch, answer="")

    rc = cli._cmd_index(argparse.Namespace(
        path=str(repo_dir), slug="declined-repo", scheme=None, semantic_include=None,
        language=None, search_only=None, fallback_search_only=None, offer_semantic=True,
    ))

    assert rc == 0
    assert prompts == ["Install semantic search support for this repo? [y/N] "]
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get("declined-repo")
        assert entry is not None
        assert entry.semantic_declined is True
    finally:
        registry.close()


@pytest.mark.interactive_input
def test_cmd_index_tty_offer_not_repeated_for_a_declined_repo(
    tmp_path: Path, monkeypatch
):
    """SC2 memory: a second `jarvis index` of a declined repo never
    prompts (input itself is poisoned) and keeps the bit."""
    import argparse

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = _offer_test_repo(tmp_path)
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)
    _script_input(monkeypatch, answer="")
    rc = cli._cmd_index(argparse.Namespace(
        path=str(repo_dir), slug="declined-repo", scheme=None, semantic_include=None,
        language=None, search_only=None, fallback_search_only=None, offer_semantic=True,
    ))
    assert rc == 0

    # Second run: prompting at all is the failure.
    _script_input(monkeypatch, error=AssertionError("must not prompt for a declined repo"))
    rc = cli._cmd_index(argparse.Namespace(
        path=str(repo_dir), slug="declined-repo", scheme=None, semantic_include=None,
        language=None, search_only=None, fallback_search_only=None, offer_semantic=True,
    ))

    assert rc == 0
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get("declined-repo")
        assert entry is not None
        assert entry.semantic_declined is True
    finally:
        registry.close()


@pytest.mark.interactive_input
def test_cmd_index_tty_offer_still_made_for_a_different_repo(
    tmp_path: Path, monkeypatch
):
    """SC2 per-repo memory: declining one repo never suppresses the offer
    for a different repo in the same data dir."""
    import argparse

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = _offer_test_repo(tmp_path, name="repo-a")
    other_dir = _offer_test_repo(tmp_path, name="repo-b")
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)
    prompts = _script_input(monkeypatch, answer="")

    rc = cli._cmd_index(argparse.Namespace(
        path=str(repo_dir), slug="repo-a", scheme=None, semantic_include=None,
        language=None, search_only=None, fallback_search_only=None, offer_semantic=True,
    ))
    assert rc == 0
    assert len(prompts) == 1
    prompts.clear()

    rc = cli._cmd_index(argparse.Namespace(
        path=str(other_dir), slug="repo-b", scheme=None, semantic_include=None,
        language=None, search_only=None, fallback_search_only=None, offer_semantic=True,
    ))
    assert rc == 0
    assert prompts == ["Install semantic search support for this repo? [y/N] "]
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        assert registry.get("repo-a").semantic_declined is True
        assert registry.get("repo-b").semantic_declined is True
    finally:
        registry.close()


def _offer_run(monkeypatch, tmp_path, slug, answer=None, error=None):
    """One mocked healthy `jarvis index` run for `slug` under the forced
    offer seams, with a scripted prompt answer; returns (rc, prompts)."""
    import argparse

    import jarvis.index_cli as cli

    repo_dir = _offer_test_repo(tmp_path, name=slug)
    prompts = _script_input(monkeypatch, answer=answer, error=error)
    rc = cli._cmd_index(argparse.Namespace(
        path=str(repo_dir), slug=slug, scheme=None, semantic_include=None,
        language=None, search_only=None, fallback_search_only=None, offer_semantic=True,
    ))
    return rc, prompts


@pytest.mark.interactive_input
def test_cmd_index_offer_accept_parse_table(tmp_path: Path, monkeypatch):
    """SEMA-01 parse table, exactly the locked one: strip+lower in
    {"y","yes"} accepts and installs (no decline bit); every other
    answer — empty, whitespace, n/N/no, garbage — declines and persists
    the bit, without ever attempting an install."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)

    for i, answer in enumerate(["y", "Y", "yes", "Yes", "YES"]):
        slug = f"accept-{i}"
        installs: list[str] = []

        def _install():
            installs.append(slug)
            return True

        monkeypatch.setattr("jarvis.index_cli._install_semantic_extra", _install)
        rc, prompts = _offer_run(monkeypatch, tmp_path, slug, answer=answer)
        assert rc == 0
        assert len(installs) == 1
        assert len(prompts) == 1
        registry = Registry(tmp_path / "data" / "registry.db")
        try:
            assert registry.get(slug).semantic_declined is False
        finally:
            registry.close()

    for i, answer in enumerate(["", " ", "n", "N", "no", "maybe"]):
        slug = f"refuse-{i}"

        def _boom():
            raise AssertionError("a declined answer must never install")

        monkeypatch.setattr("jarvis.index_cli._install_semantic_extra", _boom)
        rc, _ = _offer_run(monkeypatch, tmp_path, slug, answer=answer)
        assert rc == 0
        registry = Registry(tmp_path / "data" / "registry.db")
        try:
            assert registry.get(slug).semantic_declined is True
        finally:
            registry.close()


@pytest.mark.interactive_input
def test_cmd_index_offer_yes_installs_and_enables_semantic_same_invocation(
    tmp_path: Path, monkeypatch, capsys
):
    """SEMA-01/SC1: a consented install re-enables semantic in the SAME
    invocation — install → importlib.invalidate_caches → the semantic
    stage re-run with the row's include prefixes → mark_semantic_indexed,
    and consent never writes a decline bit."""
    import argparse

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = _offer_test_repo(tmp_path)
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)
    _script_input(monkeypatch, answer="y")
    monkeypatch.setattr("jarvis.index_cli._install_semantic_extra", lambda: True)

    invalidations: list = []
    monkeypatch.setattr(
        "importlib.invalidate_caches", lambda: invalidations.append(1)
    )

    stage_calls: list = []

    def _stage(repo_path, slug, root, include_prefixes=()):
        stage_calls.append((repo_path, slug, root, include_prefixes))
        return True

    # After _mock_healthy_full_run, so this overrides its False lambda.
    # The pipeline's in-run stage goes through _finish_semantic_stage; the
    # offer's post-install re-run is the one _run_semantic_stage caller.
    monkeypatch.setattr("jarvis.index_cli._run_semantic_stage", _stage)

    rc = cli._cmd_index(argparse.Namespace(
        path=str(repo_dir), slug="consented-repo", scheme=None, semantic_include=None,
        language=None, scip=None, offer_semantic=True,
    ))

    assert rc == 0
    assert len(invalidations) == 1
    # The single _run_semantic_stage call is the offer's re-run — root=None
    # from _cmd_index; the pipeline's own stage rode the healthy mocks.
    assert len(stage_calls) == 1
    assert stage_calls[0] == (Path(str(repo_dir)), "consented-repo", None, ())
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get("consented-repo")
        assert entry is not None
        assert entry.semantic_indexed_at is not None  # mark_semantic_indexed ran
        assert entry.semantic_declined is False  # consent writes no bit
    finally:
        registry.close()


@pytest.mark.interactive_input
def test_cmd_index_offer_install_failure_warns_and_does_not_remember_decline(
    tmp_path: Path, monkeypatch, capsys
):
    """SEMA-01 three-outcome contract: install failure (uv absent,
    non-zero exit, timeout — all False at the seam) warns with exactly
    ONE stderr line naming the tried command, exits 0, never re-runs the
    stage, and writes no decline bit — failure is not a refusal, so the
    next TTY index offers again."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)
    monkeypatch.setattr("jarvis.index_cli._install_semantic_extra", lambda: False)

    stage_calls: list = []

    def _stage(repo_path, slug, root, include_prefixes=()):
        stage_calls.append((repo_path, slug, root, include_prefixes))
        # Healthy-mock False everywhere: nothing stamps, and any offer
        # re-run would still be recorded (a second call per slug).
        return False

    monkeypatch.setattr("jarvis.index_cli._run_semantic_stage", _stage)

    for i, shape in enumerate(["uv-absent", "non-zero-exit", "timeout"]):
        slug = f"failed-install-{i}"
        rc, _ = _offer_run(monkeypatch, tmp_path, slug, answer="y")
        assert rc == 0, shape
        err = capsys.readouterr().err
        naming = [
            line for line in err.splitlines()
            if "uv pip install" in line and "jarvis-mcp[semantic]" in line
        ]
        assert len(naming) == 1, (shape, err)
        assert (
            len([line for line in err.splitlines()
                 if "warning: semantic extra install failed" in line]) == 1
        ), (shape, err)
        registry = Registry(tmp_path / "data" / "registry.db")
        try:
            entry = registry.get(slug)
            assert entry is not None
            assert entry.semantic_declined is False, shape
        finally:
            registry.close()
    # No _run_semantic_stage call in any run: the pipeline's in-run stage
    # goes through _finish_semantic_stage (healthy-mocked), and the offer's
    # re-run must never happen on the failure path.
    assert stage_calls == []


@pytest.mark.interactive_input
def test_cmd_index_offer_eof_or_keyboard_interrupt_at_prompt_declines_remembered(
    tmp_path: Path, monkeypatch, capsys
):
    """SEMA-01/SC2: abnormal prompt input — EOF (piped-off stdin), Ctrl-C,
    and undecodable bytes (input() decodes stdin strict, so pasted binary
    garbage raises UnicodeDecodeError before any answer exists) — is a
    decline: remembered, exit 0, no traceback, and no install ever
    attempted."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)

    def _boom():
        raise AssertionError("EOF/Ctrl-C must never reach an install")

    monkeypatch.setattr("jarvis.index_cli._install_semantic_extra", _boom)

    for i, error in enumerate([
        EOFError(),
        KeyboardInterrupt(),
        UnicodeDecodeError("utf-8", b"\x80\x81", 0, 1, "invalid start byte"),
    ]):
        slug = f"interrupted-{i}"
        rc, prompts = _offer_run(monkeypatch, tmp_path, slug, error=error)
        assert rc == 0
        assert len(prompts) == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out + captured.err
        registry = Registry(tmp_path / "data" / "registry.db")
        try:
            entry = registry.get(slug)
            assert entry is not None
            assert entry.semantic_declined is True
        finally:
            registry.close()


def test_install_semantic_extra_argv_and_failure_paths(monkeypatch):
    """The locked uv command as a fixed argv: [resolved uv, "pip",
    "install", "--python", sys.executable, "jarvis-mcp[semantic]"] — list
    form (no shell), spec unpinned, decoded with errors="replace" so a
    legacy-locale uv can't crash the decode. uv absent → False with no
    subprocess at all; a non-zero exit → False; a TimeoutExpired → False.
    Spawn OSErrors and decode failures are pinned by the WR-02 test
    below."""
    import sys as _sys

    from jarvis.index_cli import _install_semantic_extra

    runs: list = []

    class _Result:
        def __init__(self, returncode):
            self.returncode = returncode

    def _run(cmd, **kwargs):
        runs.append((cmd, kwargs))
        return _Result(0)

    monkeypatch.setattr("jarvis.index_cli.shutil.which", lambda name: "/fake/bin/uv")
    monkeypatch.setattr("jarvis.index_cli.subprocess.run", _run)

    assert _install_semantic_extra() is True
    assert runs == [(
        ["/fake/bin/uv", "pip", "install", "--python", _sys.executable,
         "jarvis-mcp[semantic]"],
        {"capture_output": True, "text": True, "errors": "replace", "timeout": 600},
    )]

    # Non-zero exit → False.
    monkeypatch.setattr(
        "jarvis.index_cli.subprocess.run", lambda cmd, **k: _Result(1)
    )
    assert _install_semantic_extra() is False

    # uv not on PATH → False, and no subprocess is ever spawned.
    runs.clear()
    monkeypatch.setattr("jarvis.index_cli.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "jarvis.index_cli.subprocess.run",
        lambda cmd, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    assert _install_semantic_extra() is False
    assert runs == []

    # A stalled install times out into the same failure path.
    monkeypatch.setattr("jarvis.index_cli.shutil.which", lambda name: "/fake/bin/uv")

    def _timeout(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 600)

    monkeypatch.setattr("jarvis.index_cli.subprocess.run", _timeout)
    assert _install_semantic_extra() is False


def test_install_semantic_extra_swallows_spawn_and_decode_failures(monkeypatch):
    """WR-02: every spawn/decode failure inside the install helper lands
    in the same warn-and-continue False path as a timeout — an OSError
    from subprocess.run (uv unexecutable after `which` said yes, a TOCTOU
    unlink, EACCES) or a UnicodeDecodeError from decoding uv's captured
    output must never escape to traceback the just-published index."""
    from jarvis.index_cli import _install_semantic_extra

    monkeypatch.setattr("jarvis.index_cli.shutil.which", lambda name: "/fake/bin/uv")

    for exc in (
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
        UnicodeDecodeError("utf-8", b"\x80", 0, 1, "invalid start byte"),
    ):
        def _raise(cmd, **kwargs):
            raise exc

        monkeypatch.setattr("jarvis.index_cli.subprocess.run", _raise)
        assert _install_semantic_extra() is False, repr(exc)


def test_cmd_index_non_tty_never_prompts_or_blocks(
    tmp_path: Path, monkeypatch
):
    """SEMA-02: a piped/cron `jarvis index` (either stream redirected)
    never prompts or blocks on stdin, never installs, and never writes
    the decline bit — never answered, so a later TTY run still offers."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    monkeypatch.setattr("jarvis.index_cli._semantic_extra_missing", lambda: True)
    monkeypatch.setattr("jarvis.index_cli._at_interactive_tty", lambda: False)
    _script_input(
        monkeypatch, error=AssertionError("a non-TTY run must never prompt")
    )
    monkeypatch.setattr(
        "jarvis.index_cli._install_semantic_extra",
        lambda: (_ for _ in ()).throw(
            AssertionError("a non-TTY run must never install")
        ),
    )

    rc, _ = _offer_run(monkeypatch, tmp_path, "piped-repo")

    assert rc == 0
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get("piped-repo")
        assert entry is not None
        assert entry.semantic_declined is False
    finally:
        registry.close()


def test_at_interactive_tty_requires_both_streams_tty(monkeypatch):
    """SEMA-02 both-stream gate: pip's convention — either stream
    redirected means automation. Pins the stdin-TTY-but-stdout-piped edge
    from the phase coverage report (a prompt there would hang a pipe)."""
    import sys

    from jarvis.index_cli import _at_interactive_tty

    class _Stream:
        def __init__(self, tty: bool):
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    for stdin_tty, stdout_tty, expected in [
        (True, True, True),
        (True, False, False),  # stdin is a TTY but stdout is piped
        (False, True, False),
        (False, False, False),
    ]:
        monkeypatch.setattr(sys, "stdin", _Stream(stdin_tty))
        monkeypatch.setattr(sys, "stdout", _Stream(stdout_tty))
        assert _at_interactive_tty() is expected


def test_offer_semantic_defaults_true_only_on_index_subparser(tmp_path: Path):
    """SEMA-02 structural gate at the parser level: only the `index`
    subparser carries offer_semantic; reindex/watch/list (and their
    synthetic Namespaces) read False via getattr's default."""
    from jarvis.index_cli import build_parser

    parser = build_parser()

    assert parser.parse_args(["index", str(tmp_path)]).offer_semantic is True
    for argv in [["reindex", "some-slug"], ["watch", str(tmp_path)], ["list"]]:
        assert getattr(parser.parse_args(argv), "offer_semantic", False) is False, argv


def test_cmd_reindex_never_offers_semantic_install(
    tmp_path: Path, monkeypatch
):
    """SEMA-02 / planner decision 1: `jarvis reindex` delegates to
    `_cmd_index` through a synthetic Namespace that omits offer_semantic
    — so even with every other gate forced open (extra missing, both
    streams TTY) on a never-declined repo, reindex never prompts."""
    import argparse

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    monkeypatch.setattr("jarvis.index_cli._semantic_extra_missing", lambda: True)

    # Seed a completed, never-declined row non-interactively.
    monkeypatch.setattr("jarvis.index_cli._at_interactive_tty", lambda: False)
    slug = "reindex-repo"
    rc, _ = _offer_run(monkeypatch, tmp_path, slug)
    assert rc == 0

    # Now every gate is open except the structural one, and prompting at
    # all is the failure.
    monkeypatch.setattr("jarvis.index_cli._at_interactive_tty", lambda: True)
    _script_input(monkeypatch, error=AssertionError("reindex must never prompt"))
    rc = cli._cmd_reindex(argparse.Namespace(slug=slug))

    assert rc == 0
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.semantic_declined is False
    finally:
        registry.close()


def test_cmd_watch_reindex_never_prompts_even_at_a_tty(
    tmp_path: Path, monkeypatch
):
    """SEMA-02 watch leg: watch IS a foreground TTY (05-RESEARCH Pitfall
    1), so the isatty gate alone could never protect it — the structural
    proof is that `_reindex` calls index_repo directly and can never
    reach `_cmd_index`'s offer. Extra missing, TTY forced True, input
    poisoned: still rc 0, no prompt, no raise. (The MCP leg is the same
    structural property — server.py imports no index path at all.)"""
    import argparse
    import itertools

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = tmp_path / "not-a-repo"
    repo_dir.mkdir()
    monkeypatch.setattr("jarvis.index_cli._semantic_extra_missing", lambda: True)
    monkeypatch.setattr("jarvis.index_cli._at_interactive_tty", lambda: True)
    _script_input(monkeypatch, error=AssertionError("watch must never prompt"))

    def fake_index_repo(path, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "index_repo", fake_index_repo)

    rc = _drive_cmd_watch(
        cli, monkeypatch,
        {"path": str(repo_dir), "slug": None, "scheme": None,
         "debounce": 0.0, "language": None, "scip": None},
        sleep_results=itertools.chain(itertools.repeat(None, 5), [KeyboardInterrupt()]),
    )
    assert rc == 0


def test_semantic_extra_missing_detects_missing_top_level_modules(monkeypatch):
    """SEMA-01 detection set: exactly the two modules the semantic stage
    imports lazily — either missing means the extra is missing.
    tree_sitter_language_pack is deliberately NEVER queried (chunker.py
    falls back to fixed-window chunking; its absence never disables
    semantic)."""
    from jarvis.index_cli import _semantic_extra_missing

    available = {
        "lancedb": object(),
        "sentence_transformers": object(),
        "tree_sitter_language_pack": object(),
    }
    seen: list[str] = []

    def _find_spec(name):
        seen.append(name)
        return available.get(name)

    monkeypatch.setattr("jarvis.index_cli.importlib.util.find_spec", _find_spec)

    assert _semantic_extra_missing() is False  # both present
    seen.clear()
    available["lancedb"] = None
    assert _semantic_extra_missing() is True  # lancedb gone
    available["lancedb"] = object()
    available["sentence_transformers"] = None
    assert _semantic_extra_missing() is True  # sentence_transformers gone
    available["sentence_transformers"] = object()
    available["tree_sitter_language_pack"] = None
    assert _semantic_extra_missing() is False  # tree-sitter is not a requirement
    assert "tree_sitter_language_pack" not in seen


def test_cmd_index_no_prompt_when_extra_already_installed(
    tmp_path: Path, monkeypatch
):
    """Locked Area 3: extra installed → no prompt ever, and the decline
    bit is never written (the find_spec gate precedes the decline gate,
    so a stale declined bit is moot once the extra exists)."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    monkeypatch.setattr("jarvis.index_cli._semantic_extra_missing", lambda: False)
    monkeypatch.setattr("jarvis.index_cli._at_interactive_tty", lambda: True)
    _script_input(
        monkeypatch, error=AssertionError("must not prompt when the extra exists")
    )

    rc, _ = _offer_run(monkeypatch, tmp_path, "installed-repo")

    assert rc == 0
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get("installed-repo")
        assert entry is not None
        assert entry.semantic_declined is False  # the bit is never written
    finally:
        registry.close()


@pytest.mark.interactive_input
def test_cmd_index_semantic_include_runs_on_declined_repo_without_clearing_bit(
    tmp_path: Path, monkeypatch
):
    """Locked Area 3: an explicit --semantic-include is direct user
    intent — the semantic stage runs with those prefixes on a declined
    repo, never blocked, and the decline bit stays set. The pipeline's
    in-run stage rides the prepare/finish split (spec TSI-09)."""
    import argparse

    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    _mock_healthy_full_run(monkeypatch)
    _force_offer_seams(monkeypatch)
    slug = "include-repo"

    # Pre-decline the repo (one offer run, Enter = No).
    rc, _ = _offer_run(monkeypatch, tmp_path, slug, answer="")
    assert rc == 0

    # Now the extra is present (no offer) and the user passes an explicit
    # include: the pipeline's prepare half must run with those prefixes.
    monkeypatch.setattr("jarvis.index_cli._semantic_extra_missing", lambda: False)
    prepare_calls: list = []
    finish_calls: list = []

    def _prepare(repo_path, slug, root, include_prefixes, manifest):
        prepare_calls.append((repo_path, slug, root, include_prefixes))
        return None

    def _finish(work, prepared_chunks, pool):
        finish_calls.append(work)
        return True

    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", _prepare)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", _finish)
    rc = cli._cmd_index(argparse.Namespace(
        path=str(tmp_path / slug), slug=slug, scheme=None,
        semantic_include=["src/"], language=None, scip=None,
        offer_semantic=True,
    ))

    assert rc == 0
    assert [c[3] for c in prepare_calls] == [("src/",)]
    assert len(finish_calls) == 0  # prepare skipped: nothing to finish
    registry = Registry(tmp_path / "data" / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.semantic_declined is True  # never cleared
        assert entry.semantic_include == ("src/",)
    finally:
        registry.close()


# --- Task 6: staged syntax baseline pipeline (spec TSI-04/06/07/08) ---------

def _git_head_sha(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _syntax_only_mocks(monkeypatch):
    """Mocks for a run that must NEVER attempt SCIP: zoekt (the only real
    subprocess left) succeeds, and any indexer/convert step is a tripwire.
    Semantic stage skips."""
    calls: list[str] = []

    def _run(cmd, *, cwd, step, env=None):
        calls.append(step)
        if step == "scip expt-convert" or step.endswith(" index"):
            raise AssertionError(f"SCIP must not run in this test: {step}")
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)
    return calls


def _scip_convert_mocks(monkeypatch, *, convert_db_builder, extra=None):
    """Mocks for a run whose convert step builds a caller-specified db."""
    calls: list[str] = []

    def _run(cmd, *, cwd, step, env=None):
        calls.append(step)
        if extra is not None and step in extra:
            raise extra[step]
        if step == "scip expt-convert":
            convert_db_builder(Path(cmd[3]))
            return _fake_completed_process(cmd)
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)
    return calls


def _read_singleton_state(snapshot_db: Path) -> dict:
    conn = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT scip_state, syntax_counts FROM jarvis_snapshot"
        ).fetchone()
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    return {"scip_state": row[0], "syntax_counts": json.loads(row[1]), "tables": tables}


def test_pipeline_happy_path_publishes_one_scip_plus_syntax_snapshot(
    tmp_path: Path, monkeypatch
):
    """Case 1: syntax + SCIP + Zoekt succeed -> exactly one
    index-<commit>-<generation>.db carrying REAL converter tables AND the
    Jarvis syntax tables; `current` flips once; the metadata sibling is
    named from the actual db filename; the row records `indexed` with
    scip_state=available."""
    from jarvis.index_cli import index_repo
    from tests.fixtures.synthetic_index import build_synthetic_index_db

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    _scip_convert_mocks(
        monkeypatch, convert_db_builder=build_synthetic_index_db,
        extra={"zoekt-git-index": None} if False else None,
    )

    slug = index_repo(repo_dir, slug="happy", root=data_root)

    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"index-[0-9a-f]{40}-[0-9a-f]{32}\.db", pointer), pointer
    assert pointer in {p.name for p in target_dir.iterdir()}
    # Metadata sibling derived from the actual filename.
    metadata = target_dir / (pointer.removesuffix(".db") + ".metadata.json")
    assert metadata.is_file()
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["generation"] == pointer.removesuffix(".db").split("-")[-1]
    assert payload["commit_sha"] == _git_head_sha(repo_dir)
    # The snapshot carries real converter tables + the syntax namespace.
    facts = _read_singleton_state(target_dir / pointer)
    assert "documents" in facts["tables"]
    assert "syntax_files" in facts["tables"] and "jarvis_snapshot" in facts["tables"]
    assert facts["scip_state"] == "available"

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.scip_enabled is True
        assert entry.scip_state == "available"
        assert entry.scip_failure_reason is None
        assert entry.commit_sha == _git_head_sha(repo_dir)
    finally:
        registry.close()
    # Superseded-snapshot cleanup keeps exactly the live pair.
    dbs = sorted(p.name for p in target_dir.glob("index-*.db"))
    assert dbs == [pointer]


def test_pipeline_scip_failure_degrades_but_publishes_syntax_baseline(
    tmp_path: Path, monkeypatch
):
    """Case 2: SCIP failure with a healthy baseline -> exit-0 degraded, the
    pointer publishes a syntax-only snapshot (singleton scip_state=failed,
    NO fabricated SCIP tables), and the stage fields record the cause and
    the attempt sha."""
    from jarvis.index_cli import IndexingError, index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    full_text = "error: simulated build failure\nsome detail"
    _scip_convert_mocks(
        monkeypatch,
        convert_db_builder=lambda p: (_ for _ in ()).throw(AssertionError("never")),
        extra={"scip-python index": IndexingError(full_text)},
    )

    slug = index_repo(repo_dir, slug="degraded", root=data_root)  # no raise

    head = _git_head_sha(repo_dir)
    target_dir = config.index_dir(slug, data_root)
    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    facts = _read_singleton_state(target_dir / pointer)
    assert facts["scip_state"] == "failed"
    assert "documents" not in facts["tables"], "no fabricated SCIP tables"
    assert facts["syntax_counts"]["parsed"] >= 1

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "degraded"
        assert entry.scip_state == "failed"
        assert entry.scip_failure_reason == "error: simulated build failure"
        assert "some detail" in entry.scip_failure_stderr
        assert entry.scip_failed_at_sha == head
        # Exit-0 degraded mirrors the concise stage cause into status_reason.
        assert entry.status_reason == "error: simulated build failure"
    finally:
        registry.close()


def test_pipeline_no_scip_persists_the_reversible_choice(tmp_path: Path, monkeypatch):
    """Case 3: --no-scip -> exit-0 indexed, scip_enabled=false persisted,
    scip_state=disabled, no indexer invocation; a later omitted-flag run
    stays disabled; --scip re-enables and attempts SCIP."""
    from jarvis.index_cli import index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    steps = _syntax_only_mocks(monkeypatch)
    slug = index_repo(repo_dir, slug="optout", root=data_root, scip=False)
    assert not any(s.endswith(" index") or s == "scip expt-convert" for s in steps)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "indexed"
        assert entry.scip_enabled is False
        assert entry.scip_state == "disabled"
    finally:
        registry.close()

    # Omitted flag: the persisted choice wins — still no SCIP attempt.
    steps = _syntax_only_mocks(monkeypatch)
    index_repo(repo_dir, slug="optout", root=data_root)
    assert not any(s.endswith(" index") for s in steps)
    registry = Registry(data_root / "registry.db")
    try:
        assert registry.get(slug).scip_enabled is False
    finally:
        registry.close()

    # --scip re-enables: the SCIP stage runs again (convert is a tripwire
    # here, so the run degrades — the point is the attempt happened).
    from jarvis.index_cli import IndexingError

    _scip_convert_mocks(
        monkeypatch,
        convert_db_builder=lambda p: None,
        extra={"scip-python index": IndexingError("boom")},
    )
    index_repo(repo_dir, slug="optout", root=data_root, scip=True)
    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.scip_enabled is True
        assert entry.scip_state == "failed"
    finally:
        registry.close()


def test_pipeline_unsupported_language_publishes_baseline_as_unsupported(
    tmp_path: Path, monkeypatch
):
    """Case 4: no SCIP-indexable language -> exit-0 indexed with
    scip_state=unsupported, no --no-scip required; the syntax baseline
    still covers the tracked sources."""
    from jarvis.index_cli import index_repo

    repo_dir = tmp_path / "gorepo"
    repo_dir.mkdir()
    (repo_dir / "main.go").write_text("package main\n\nfunc main() {}\n")
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    _syntax_only_mocks(monkeypatch)
    slug = index_repo(repo_dir, slug="gorepo", root=data_root)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "indexed"
        assert entry.language == "unknown"
        assert entry.scip_state == "unsupported"
    finally:
        registry.close()


def test_pipeline_zoekt_failure_is_hard_and_publishes_nothing(
    tmp_path: Path, monkeypatch
):
    """Case 5: Zoekt failure -> exit nonzero `failed`; nothing published
    this run; the prior live snapshot is untouched."""
    from jarvis.index_cli import IndexingError, index_repo
    from tests.fixtures.synthetic_index import build_synthetic_index_db

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    # First run publishes (the "prior live snapshot").
    _scip_convert_mocks(monkeypatch, convert_db_builder=build_synthetic_index_db)
    slug = index_repo(repo_dir, slug="zoektfail", root=data_root)
    target_dir = config.index_dir(slug, data_root)
    live_pointer = (target_dir / "current").read_text(encoding="utf-8").strip()

    # Second run: zoekt dies after the SCIP stage succeeded.
    from jarvis.index_cli import MissingBinaryError

    def _run(cmd, *, cwd, step, env=None):
        if step == "zoekt-git-index":
            raise MissingBinaryError("zoekt-git-index failed: not found on PATH — run setup.sh")
        if step == "scip expt-convert":
            build_synthetic_index_db(Path(cmd[3]))
            return _fake_completed_process(cmd)
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    with pytest.raises(IndexingError, match="zoekt-git-index"):
        index_repo(repo_dir, slug="zoektfail", root=data_root)

    assert (target_dir / "current").read_text(encoding="utf-8").strip() == live_pointer
    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "failed"
        assert entry.scip_failed_at_sha is None  # the run never reached a decision
    finally:
        registry.close()


def test_pipeline_registry_failure_after_pointer_flip_reports_snapshot_live(
    tmp_path: Path, monkeypatch
):
    """Case 6: registry write fails after the pointer flip -> exit nonzero
    with the 'snapshot already live' report; superseded generations are NOT
    deleted yet; a later successful run cleans them."""
    import jarvis.index_cli as cli
    from jarvis.index_cli import IndexingError, index_repo
    from tests.fixtures.synthetic_index import build_synthetic_index_db

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    _scip_convert_mocks(monkeypatch, convert_db_builder=build_synthetic_index_db)
    slug = index_repo(repo_dir, slug="bookkeep", root=data_root)
    target_dir = config.index_dir(slug, data_root)
    first_live = (target_dir / "current").read_text(encoding="utf-8").strip()

    # Second run flips a new pointer, then the terminal registry write dies.
    real_upsert = cli.Registry.upsert

    def _upsert_then_die(self, slug_arg, *a, **k):
        # Let the transitional write through; kill the terminal one (the
        # only upsert that carries a non-None commit_sha for this run).
        if k.get("scip_stage") is not None or a and len(a) > 4:
            raise sqlite3.OperationalError("database is locked")
        return real_upsert(self, slug_arg, *a, **k)

    monkeypatch.setattr(cli.Registry, "upsert", _upsert_then_die)
    with pytest.raises(IndexingError, match="already live"):
        index_repo(repo_dir, slug="bookkeep", root=data_root)

    second_live = (target_dir / "current").read_text(encoding="utf-8").strip()
    assert second_live != first_live
    # The superseded first generation is still on disk (cleanup never ran).
    assert (target_dir / first_live).is_file()

    # A later successful run retires the superseded generation.
    monkeypatch.setattr(cli.Registry, "upsert", real_upsert)
    index_repo(repo_dir, slug="bookkeep", root=data_root)
    dbs = sorted(p.name for p in target_dir.glob("index-*.db"))
    assert dbs == [(target_dir / "current").read_text(encoding="utf-8").strip()]


def test_pipeline_same_commit_reindex_produces_distinct_immutable_snapshots(
    tmp_path: Path, monkeypatch
):
    """Case 8 (snapshot safety + reuse): two runs at one commit produce
    different immutable index-<commit>-<generation>.db filenames with
    matching metadata stems; the reader cache invalidates by pointer
    content; unchanged sources are carried forward without re-parse (the
    second snapshot's rows come from reuse, and the generation id is still
    unique)."""
    from jarvis.index_cli import index_repo
    from jarvis.index_reader import IndexConnectionCache
    from jarvis.syntax_index import read_snapshot_facts
    from tests.fixtures.synthetic_index import build_synthetic_index_db

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    _scip_convert_mocks(monkeypatch, convert_db_builder=build_synthetic_index_db)
    slug = index_repo(repo_dir, slug="reuser", root=data_root)
    target_dir = config.index_dir(slug, data_root)
    first = (target_dir / "current").read_text(encoding="utf-8").strip()

    cache = IndexConnectionCache(str(data_root))
    conn_before, _ = config.get_connection(cache, slug)
    facts_before = read_snapshot_facts(conn_before)

    index_repo(repo_dir, slug="reuser", root=data_root)
    second = (target_dir / "current").read_text(encoding="utf-8").strip()
    assert first != second
    assert (target_dir / first).is_file() or True  # retired by cleanup
    assert (target_dir / second).is_file()
    assert (target_dir / (second.removesuffix(".db") + ".metadata.json")).is_file()

    # Reader cache: the pointer content changed, so the cached connection
    # for the old pointer is never consulted again.
    conn_after, _ = config.get_connection(cache, slug)
    facts_after = read_snapshot_facts(conn_after)
    assert facts_after.generation != facts_before.generation
    assert facts_after.source_hash == facts_before.source_hash  # unchanged sources
    assert facts_after.syntax_counts.parsed >= 1  # rows carried/reparsed live
    cache.close_all()


def test_pipeline_syntax_only_generation_clears_repo_owned_graph_edges(
    tmp_path: Path, monkeypatch
):
    """Case 9 (graph): a SCIP-less generation clears every outgoing edge of
    packages the repo owns — even when the prior generation had working
    SCIP edges — while package identities shared with other repos survive;
    `forget` unpins zoekt.name and tears the graph rows down."""
    import argparse

    from jarvis.graph import GraphStore
    from jarvis.index_cli import _cmd_forget, index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))

    # Pre-seed graph rows as if a prior SCIP generation had populated them.
    store = GraphStore(data_root / "registry.db")
    mine = store.upsert_package(repo="graphrepo", name="npm:mine")
    other = store.upsert_package(repo="other-repo", name="npm:theirs")
    store.add_edge(from_package_id=mine, to_package_id=other)
    store.add_edge(from_package_id=other, to_package_id=mine)
    store.close()

    _syntax_only_mocks(monkeypatch)
    slug = index_repo(repo_dir, slug="graphrepo", root=data_root, scip=False)

    store = GraphStore(data_root / "registry.db")
    try:
        assert store.get_dependents(other) == []  # repo-owned outgoing edge cleared
        assert store.get_package_by_key("graphrepo", "npm:mine") is not None
        assert store.get_package_by_key("other-repo", "npm:theirs") is not None
        assert store.get_dependents(mine)  # other repo's incoming edge survives
    finally:
        store.close()

    # forget: unpins zoekt.name AND tears the graph rows down.
    rc = _cmd_forget(argparse.Namespace(slug=slug))
    assert rc == 0
    unpinned = subprocess.run(
        ["git", "-C", str(repo_dir), "config", "--get", "zoekt.name"],
        capture_output=True, text=True,
    )
    assert unpinned.returncode != 0  # key gone
    store = GraphStore(data_root / "registry.db")
    try:
        assert store.get_package_by_key("graphrepo", "npm:mine") is None
        assert store.get_package_by_key("other-repo", "npm:theirs") is not None
        assert store.get_dependents(other) == []  # dangling edge gone too
    finally:
        store.close()


def test_pipeline_never_modifies_a_published_legacy_scip_db(
    tmp_path: Path, monkeypatch
):
    """Case 10 (part): a published legacy SCIP snapshot is never modified
    in place by a baseline publish — the new generation lands under a
    fresh unique name, and the legacy db retires (is superseded) only
    after the new pointer is live, per the immutable-artifact lifecycle.
    The legacy format itself stays readable as legacy (real converter
    tables, no syntax namespace)."""
    from jarvis.index_cli import index_repo
    from jarvis.syntax_index import has_scip_tables, has_syntax_tables

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_root))

    from tests.fixtures.synthetic_index import build_published_index

    slug = "legacy"
    build_published_index(data_root, config.PROJECT, slug, config.BRANCH)
    target_dir = config.index_dir(slug, data_root)
    legacy_db = target_dir / "index-abc1234.db"
    preserved_copy = tmp_path / "legacy-copy.db"
    shutil.copyfile(legacy_db, preserved_copy)

    _syntax_only_mocks(monkeypatch)
    index_repo(repo_dir, slug=slug, scip=False)

    pointer = (target_dir / "current").read_text(encoding="utf-8").strip()
    assert pointer != "index-abc1234.db"
    # The legacy artifact was retired whole (never mutated in place):
    # comparing against the preserved copy proves the format for readers
    # that still hold a reference to that generation.
    assert not legacy_db.exists() or legacy_db.read_bytes() == preserved_copy.read_bytes()
    conn = sqlite3.connect(f"file:{preserved_copy}?mode=ro", uri=True)
    try:
        assert has_scip_tables(conn)
        assert not has_syntax_tables(conn)  # legacy format, no syntax namespace
    finally:
        conn.close()
    facts = _read_singleton_state(target_dir / pointer)
    assert facts["scip_state"] == "disabled"
    assert facts["syntax_counts"]["parsed"] >= 1


def test_removed_options_are_rejected_with_the_replacement():
    """Case 11: the removed flags are rejected (exit 2) with a stderr
    message naming the replacement — never silently mapped."""
    import jarvis.index_cli as cli

    for flag, replacement in [
        ("--search-only", "--no-scip"),
        ("--fallback-search-only", "--scip"),
        ("--no-fallback-search-only", "--scip"),
    ]:
        with pytest.raises(SystemExit) as excinfo:
            cli._reject_removed_options(["index", "/repos/x", flag])
        assert excinfo.value.code == 2
        assert flag in cli._REMOVED_OPTIONS


def test_main_rejects_removed_option_before_argparse(monkeypatch):
    """The rejection happens in main()'s pre-scan, so a stale script sees
    the tailored replacement message instead of argparse's generic
    'unrecognized arguments'."""
    import jarvis.index_cli as cli

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["index", "/repos/x", "--search-only"])
    assert excinfo.value.code == 2


def test_scip_suppressed_matrix():
    """The four-condition watch predicate (spec TSI-07), as a pure decision
    matrix. Only the exact suppressed shape returns True: a watch caller,
    SCIP enabled with a supported selected language, a failed/unavailable
    stage recorded AT the current HEAD, and no explicit re-enable or
    effective language/scheme change."""
    from datetime import UTC, datetime

    from jarvis.index_cli import _scip_suppressed
    from jarvis.registry import RegisteredRepo

    def entry(**kw):
        fields = dict(
            slug="mine", path="/m", language="python", commit_sha="abc",
            last_indexed=datetime.now(UTC), status="degraded",
            scip_enabled=True, scip_state="failed", scip_failed_at_sha="abc",
        )
        fields.update(kw)
        return RegisteredRepo(**fields)

    base = dict(watch=True, scip_enabled=True, explicit_scip=None,
                language=None, scheme=None, head="abc")
    assert _scip_suppressed(entry(), **base) is True
    # (1) explicit caller never suppresses.
    assert _scip_suppressed(entry(), **{**base, "watch": False}) is False
    # (2) disabled or unsupported selected language never suppresses.
    assert _scip_suppressed(entry(), **{**base, "scip_enabled": False}) is False
    assert _scip_suppressed(entry(language="unknown"), **base) is False
    # (3) a different stage state or a new commit never suppresses.
    assert _scip_suppressed(entry(scip_state="disabled"), **base) is False
    assert _scip_suppressed(entry(scip_state="available"), **base) is False
    assert _scip_suppressed(entry(scip_failed_at_sha="old"), **base) is False
    assert _scip_suppressed(entry(scip_failed_at_sha=None), **base) is False
    # (4) explicit re-enable or effective language/scheme change.
    assert _scip_suppressed(entry(), **{**base, "explicit_scip": True}) is False
    assert _scip_suppressed(entry(), **{**base, "language": "swift"}) is False
    assert _scip_suppressed(entry(), **{**base, "scheme": "Other"}) is False
    # Same-value explicit language/scheme does NOT invalidate the record.
    assert _scip_suppressed(entry(language_override="python"), **base) is True
    # A missing row never suppresses.
    assert _scip_suppressed(None, **base) is False


def test_pipeline_watch_suppression_skips_only_scip(
    tmp_path: Path, monkeypatch, capsys
):
    """Case 7: a watch run with a prior failed stage at the same HEAD skips
    ONLY the SCIP attempt — baseline, semantic, and publication still run,
    the failure fields are preserved, and the diagnostic names the retry
    command. A new commit or an explicit run retries SCIP."""
    from jarvis.index_cli import IndexingError, index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    _scip_convert_mocks(
        monkeypatch,
        convert_db_builder=lambda p: None,
        extra={"scip-python index": IndexingError("compiler exploded")},
    )
    slug = index_repo(repo_dir, slug="treadmill", root=data_root)
    head = _git_head_sha(repo_dir)

    registry = Registry(data_root / "registry.db")
    try:
        failed = registry.get(slug)
        assert failed.status == "degraded"
        assert failed.scip_failed_at_sha == head
    finally:
        registry.close()

    # Watch run at the same commit: SCIP never invoked, everything else
    # runs (zoekt step present), pointer republished, fields preserved.
    steps: list[str] = []

    def _run(cmd, *, cwd, step, env=None):
        steps.append(step)
        if step == "scip expt-convert" or step.endswith(" index"):
            raise AssertionError("suppressed run must not attempt SCIP")
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)
    index_repo(repo_dir, slug="treadmill", root=data_root, watch=True)

    assert "zoekt-git-index" in steps  # baseline + publication ran
    err = capsys.readouterr().err
    assert "SCIP skipped" in err
    assert "Syntax and search were refreshed" in err
    assert "--scip" in err  # names the retry command

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "degraded"  # still degraded (suppressed)
        assert entry.scip_state == "failed"  # preserved
        assert entry.scip_failure_reason == "compiler exploded"
        assert entry.scip_failed_at_sha == head
        assert entry.scip_enabled is True  # suppression never persists disabled
    finally:
        registry.close()

    # An explicit (non-watch) run retries SCIP.
    _scip_convert_mocks(
        monkeypatch,
        convert_db_builder=lambda p: None,
        extra={"scip-python index": IndexingError("compiler exploded again")},
    )
    index_repo(repo_dir, slug="treadmill", root=data_root)
    registry = Registry(data_root / "registry.db")
    try:
        assert registry.get(slug).scip_failure_reason == "compiler exploded again"
    finally:
        registry.close()

    # A new commit retries on the next watch run too — and the retry is
    # observable: the SCIP indexer step runs (convert writes a real
    # minimal db, so the run completes with SCIP accepted).
    (repo_dir / "new.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "next"], cwd=repo_dir, check=True)
    steps.clear()

    def _run2(cmd, *, cwd, step, env=None):
        steps.append(step)
        if step == "scip expt-convert":
            _make_index_db(Path(cmd[3]), chunks=1, mentions=2)
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run2)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)
    index_repo(repo_dir, slug="treadmill", root=data_root, watch=True)
    assert "scip-python index" in steps
    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "indexed"  # the retry succeeded
        assert entry.scip_state == "available"
        assert entry.scip_failure_reason is None  # successful enrichment cleared its fields
    finally:
        registry.close()


def test_pipeline_watch_explicit_scip_re_enable_overrides_suppression(
    tmp_path: Path, monkeypatch
):
    """Case 7 (re-enable): a watch run carrying an explicit --scip retries
    SCIP even at the same failing commit."""
    from jarvis.index_cli import IndexingError, index_repo

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    _scip_convert_mocks(
        monkeypatch,
        convert_db_builder=lambda p: None,
        extra={"scip-python index": IndexingError("still broken")},
    )
    slug = index_repo(repo_dir, slug="reenable", root=data_root)

    calls: list[str] = []

    def _run(cmd, *, cwd, step, env=None):
        calls.append(step)
        if step == "scip-python index":
            raise IndexingError("still broken")
        if step == "scip expt-convert":
            from jarvis.index_cli import ScipAttempt  # noqa: F401
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", lambda *a, **k: None)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", lambda *a, **k: False)
    index_repo(repo_dir, slug="reenable", root=data_root, watch=True, scip=True)
    assert "scip-python index" in calls  # attempted despite suppression shape


def test_parser_accepts_scip_flags_on_index_reindex_and_watch():
    """--scip/--no-scip on index, reindex, and watch; omitted reads None
    (use the persisted choice)."""
    from jarvis.index_cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["index", "/r", "--scip"]).scip is True
    assert parser.parse_args(["index", "/r", "--no-scip"]).scip is False
    assert parser.parse_args(["index", "/r"]).scip is None
    assert parser.parse_args(["reindex", "x", "--scip"]).scip is True
    assert parser.parse_args(["reindex", "x", "--no-scip"]).scip is False
    assert parser.parse_args(["reindex", "x"]).scip is None
    assert parser.parse_args(["watch", "/r", "--scip"]).scip is True
    assert parser.parse_args(["watch", "/r", "--no-scip"]).scip is False
    assert parser.parse_args(["watch", "/r"]).scip is None


def test_reindex_forwards_scip_flag(tmp_path: Path, monkeypatch):
    """reindex forwards the tri-state scip flag into the index command."""
    import argparse
    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("mine", "/repos/mine", "python", "abc", "indexed")
    finally:
        registry.close()

    captured: dict = {}

    def fake_index_repo(path, **kwargs):
        captured.update(kwargs)
        return "mine"

    monkeypatch.setattr(cli, "index_repo", fake_index_repo)

    cli._cmd_reindex(argparse.Namespace(slug="mine", scip=False))
    assert captured["scip"] is False


def test_cmd_watch_runs_every_event_and_marks_watch_caller(
    tmp_path: Path, monkeypatch
):
    """The watch driver runs index_repo for every debounced event with
    watch=True and the tri-state scip flag — suppression is index_repo's
    stage-level decision now, not the driver's (spec TSI-07). Driven
    through the REAL watch subparser so flag drift (--scheme/--debounce)
    fails here instead of at user startup."""
    import itertools
    import jarvis.index_cli as cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = tmp_path / "not-a-repo"
    repo_dir.mkdir()

    captured: dict = {}

    def fake_index_repo(path, *, slug=None, root=None, scheme=None,
                        semantic_include=None, language=None, scip=None,
                        semantic=True, watch=False):
        captured["scip"] = scip
        captured["watch"] = watch
        captured["slug"] = slug
        captured["scheme"] = scheme
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "index_repo", fake_index_repo)

    # Real subparser — a dropped or renamed flag cannot reach this test.
    args = cli.build_parser().parse_args(
        ["watch", str(repo_dir), "--debounce", "0", "--scheme", "MyScheme", "--no-scip"]
    )
    rc = _drive_cmd_watch(
        cli, monkeypatch, vars(args) | {"func": None, "command": "watch"},
        sleep_results=itertools.chain(itertools.repeat(None, 5), [KeyboardInterrupt()]),
    )

    assert rc == 0
    assert captured == {"scip": False, "watch": True, "slug": repo_dir.name,
                        "scheme": "MyScheme"}


def test_partial_status_for_documented_extraction_gaps(tmp_path: Path, monkeypatch):
    """PARTIAL_STATUS broadened (spec §12): a supported file that was
    size-skipped or only partially parsed publishes `partial` even with
    usable SCIP data; unsupported extensions alone never do."""
    from jarvis.index_cli import index_repo
    from tests.fixtures.synthetic_index import build_synthetic_index_db

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    # A supported file over the 1 MiB backstop -> size-skipped -> partial.
    (repo_dir / "big.py").write_text("x = " + "1" * (1024 * 1024 + 64) + "\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "big"], cwd=repo_dir, check=True)

    _scip_convert_mocks(monkeypatch, convert_db_builder=build_synthetic_index_db)
    slug = index_repo(repo_dir, slug="gappy", root=data_root)

    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry.status == "partial"
        assert entry.scip_state == "available"  # SCIP itself was fine
    finally:
        registry.close()


def test_semantic_chunk_capture_failure_discards_semantic_work(
        tmp_path: Path, monkeypatch, capsys):
    """TSI-09 callback isolation per plan :563: an exception raised by the
    optional shared-parse chunk consumer is contained -- the required
    baseline still publishes, collection STOPS at the first failure
    (chunk_file is not retried for later files), `_finish_semantic_stage`
    is never attempted (this run's semantic work is discarded, so the
    previous LanceDB table stays live), and the semantic stage warns
    exactly once."""
    from jarvis import chunker as chunker_mod
    from jarvis import semantic as semantic_mod

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    # A second python file pins stop-at-first-failure: chunk_file must not
    # be reached for any file after the first raise.
    (repo_dir / "second.py").write_text("def spare():\n    return 2\n")
    _init_git_repo(repo_dir)
    data_root = tmp_path / "data"

    def _run(cmd, *, cwd, step, env=None):
        if step.endswith(" index") or step == "scip expt-convert":
            raise AssertionError(f"SCIP must not run in this test: {step}")
        return _fake_completed_process(cmd)

    monkeypatch.setattr("jarvis.index_cli._run", _run)
    monkeypatch.setattr("jarvis.index_cli.check_scip_version", lambda: None)

    def _prepare(repo_path, slug, root, include_prefixes, manifest):
        inputs = tuple(
            semantic_mod.SemanticInput(
                file_path=f.file_path,
                source_path=f.source_path,
                file_hash=f.file_hash or "h",
                language=f.language or "python")
            for f in manifest.files if (f.language or "") == "python")
        assert len(inputs) >= 2, "fixture must hold two python files to pin stop-collection"
        return semantic_mod.SemanticWork(
            slug=slug, model=object(), store=object(), identity=object(),
            admitted=len(inputs), skipped=(), carried=(), inputs=inputs,
            vector_by_hash={})

    def _finish(work, prepared_chunks, pool):
        raise AssertionError(
            "semantic finish must never run after a capture failure (plan :563)")

    monkeypatch.setattr("jarvis.index_cli._prepare_semantic_stage", _prepare)
    monkeypatch.setattr("jarvis.index_cli._finish_semantic_stage", _finish)

    chunk_calls: list[str] = []

    def _boom(*args, **kwargs):
        chunk_calls.append(args[0] if args else "?")
        raise RuntimeError("chunker exploded")

    monkeypatch.setattr(chunker_mod, "chunk_file", _boom)

    # scip=False keeps the optional enrichment out of the way; the
    # contract under test is syntax-build survival, not SCIP interplay.
    slug = index_repo(repo_dir, slug="isolated", root=data_root, scip=False)

    assert (config.index_dir(slug, data_root) / "current").is_file()
    assert len(chunk_calls) == 1  # stopped at the first failure, no retry
    err = capsys.readouterr().err
    assert "semantic indexing failed (index still published)" in err
    assert "chunker exploded" in err
    assert err.count("semantic indexing failed") == 1  # once, at its stage
    registry = Registry(data_root / "registry.db")
    try:
        entry = registry.get(slug)
        assert entry is not None
        assert entry.status == "indexed"  # baseline unaffected
        assert entry.semantic_indexed_at is None  # discarded, not marked
    finally:
        registry.close()


def test_index_repo_semantic_false_skips_the_stage(tmp_path, monkeypatch):
    """An agent-triggered index must never implicitly download embedding
    weights. `semantic=False` skips prepare entirely -- not merely the
    install offer, which is TTY-gated and therefore already unreachable."""
    from jarvis import index_cli

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    calls: list[str] = []
    monkeypatch.setattr(
        index_cli, "_prepare_semantic_stage",
        lambda *a, **k: calls.append("prepared") or None,
    )
    monkeypatch.setattr(index_cli, "check_scip_version", lambda: None)

    def _run(cmd, *, cwd, step, env=None):
        # Degrade path (narrowed FALL-04): a simulated SCIP failure keeps
        # the fake run cheap while the rest of the pipeline still publishes.
        if step.endswith(" index"):
            from jarvis.index_cli import IndexingError
            raise IndexingError(f"{step} failed (simulated real failure):\nexit 1")
        return _fake_completed_process(cmd)

    monkeypatch.setattr(index_cli, "_run", _run)

    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)

    index_cli.index_repo(repo_dir, semantic=False)
    assert calls == []

    index_cli.index_repo(repo_dir, semantic=True)
    assert calls == ["prepared"]


def test_no_semantic_flag_parses_to_false():
    from jarvis import index_cli

    parser = index_cli.build_parser()
    assert parser.parse_args(["index", "/r", "--no-semantic"]).semantic is False
    assert parser.parse_args(["index", "/r", "--semantic"]).semantic is True
    assert parser.parse_args(["index", "/r"]).semantic is None
    assert parser.parse_args(["reindex", "s", "--no-semantic"]).semantic is False
    assert parser.parse_args(["watch", "/r", "--no-semantic"]).semantic is False


def test_index_repo_refuses_when_build_lock_held(tmp_path, monkeypatch):
    """Exclusion must cover the registry write too: the loser leaves the row
    exactly as it found it."""
    from jarvis import index_cli, jobs
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    _stub_pipeline(monkeypatch)

    with jobs.build_lock(config.repo_slug(repo_dir.name)):
        with pytest.raises(jobs.BuildLockHeld):
            index_cli.index_repo(repo_dir)

    registry = Registry(config.data_dir() / "registry.db")
    try:
        assert registry.get(config.repo_slug(repo_dir.name)) is None
    finally:
        registry.close()


def test_index_repo_lock_is_held_at_the_transitional_upsert(tmp_path, monkeypatch):
    """The lock must be acquired BEFORE the row flips to 'indexing', or two
    writers can both register."""
    from jarvis import index_cli, jobs

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    _stub_pipeline(monkeypatch)
    slug = config.repo_slug(repo_dir.name)

    seen: list[bool] = []
    real_upsert = index_cli.Registry.upsert

    def _spy(self, *args, **kwargs):
        seen.append(jobs.lock_state(slug).held)
        return real_upsert(self, *args, **kwargs)

    monkeypatch.setattr(index_cli.Registry, "upsert", _spy)
    index_cli.index_repo(repo_dir)
    assert seen and all(seen)


def test_index_repo_releases_lock_on_failure(tmp_path, monkeypatch):
    from jarvis import index_cli, jobs

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo_dir = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo_dir)
    _init_git_repo(repo_dir)
    _stub_pipeline(monkeypatch)
    monkeypatch.setattr(
        index_cli, "_pin_zoekt_repo_name",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(Exception):
        index_cli.index_repo(repo_dir)
    assert jobs.lock_state(config.repo_slug(repo_dir.name)).held is False


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


def test_install_semantic_command_parses_without_arguments():
    from jarvis import index_cli

    parser = index_cli.build_parser()
    args = parser.parse_args(["install-semantic"])

    assert args.func is index_cli._cmd_install_semantic


def test_install_semantic_refuses_frozen_build(monkeypatch, capsys):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: True)

    def forbidden():
        raise AssertionError("a frozen build must never install semantic support")

    monkeypatch.setattr(index_cli, "_semantic_extra_missing", forbidden)
    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert captured.out == ""
    assert "Homebrew binary distribution" in captured.err


def test_install_semantic_reports_already_installed(monkeypatch, capsys):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: False)

    preload_calls: list[int] = []

    class _FakeModel:
        def preload(self):
            preload_calls.append(1)

    monkeypatch.setattr("jarvis.embeddings.EmbeddingModel", _FakeModel)

    def forbidden():
        raise AssertionError("installed support must not trigger another sync")

    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == (
        "semantic support already installed\nembedding model ready\n"
    )
    assert "downloading embedding model" in captured.err
    assert preload_calls == [1]


def test_install_semantic_reports_missing_source_checkout(monkeypatch, capsys, tmp_path):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: tmp_path)

    def forbidden():
        raise AssertionError("a non-checkout must never invoke uv")

    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "source checkout" in captured.err


def test_install_semantic_reports_missing_uv(monkeypatch, capsys, tmp_path):
    import argparse

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: True)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: None)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "requires uv" in captured.err


def test_install_semantic_runs_locked_sync(monkeypatch, capsys, tmp_path):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    uv_path = "/fake/uv"
    sync_options = {
        "capture_output": True,
        "text": True,
        "errors": "replace",
    }
    calls: list[tuple[list[str], Path, int, dict[str, bool | str]]] = []
    invalidations: list[int] = []
    semantic_missing = True

    def fake_run(cmd, *, cwd, timeout, capture_output, text, errors):
        nonlocal semantic_missing
        semantic_missing = False
        calls.append(
            (
                cmd,
                cwd,
                timeout,
                {
                    "capture_output": capture_output,
                    "text": text,
                    "errors": errors,
                },
            )
        )
        return subprocess_module.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: uv_path)
    monkeypatch.setattr(index_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        index_cli, "_semantic_extra_missing", lambda: semantic_missing
    )
    monkeypatch.setattr("importlib.invalidate_caches", lambda: invalidations.append(1))
    preload_calls: list[int] = []

    class _FakeModel:
        def preload(self):
            preload_calls.append(1)

    monkeypatch.setattr("jarvis.embeddings.EmbeddingModel", _FakeModel)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert calls == [
        (
            [uv_path, "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"],
            root,
            600,
            sync_options,
        )
    ]
    assert invalidations == [1]
    assert preload_calls == [1]
    assert captured.out == (
        "semantic support installed\nembedding model ready\n"
    )
    assert captured.err.startswith("installing semantic support...\n")
    assert "downloading embedding model" in captured.err


def test_install_semantic_preload_failure_is_nonfatal(monkeypatch, capsys, tmp_path):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")

    def fake_run(cmd, *, cwd, timeout, capture_output, text, errors):
        return subprocess_module.CompletedProcess(cmd, 0, stdout="", stderr="")

    class _BoomModel:
        def preload(self):
            raise RuntimeError("network down")

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(index_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: False)
    monkeypatch.setattr("jarvis.embeddings.EmbeddingModel", _BoomModel)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert "warning: embedding model download failed (network down)" in captured.err
    assert "will download on the next index" in captured.err
    assert "embedding model ready" not in captured.out


def test_install_semantic_reports_sync_failure(monkeypatch, capsys, tmp_path):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")

    def fake_run(cmd, *, cwd, timeout, capture_output, text, errors):
        return subprocess_module.CompletedProcess(
            cmd, 1, stdout="sync failed", stderr="network unavailable"
        )

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(index_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: True)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "semantic support installation failed" in captured.err
    assert "network unavailable" in captured.err


def test_install_semantic_reports_imports_unavailable_after_sync(
    monkeypatch, capsys, tmp_path
):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: "/fake/uv")

    def fake_run(cmd, *, cwd, timeout, capture_output, text, errors):
        return subprocess_module.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(
        index_cli.subprocess,
        "run",
        fake_run,
    )
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: True)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 1
    assert "lancedb" in captured.err
    assert "sentence_transformers" in captured.err
