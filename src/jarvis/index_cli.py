"""`jarvis` CLI: capture tracked sources -> build the tree-sitter syntax
baseline -> optionally enrich with SCIP -> zoekt-git-index -> optional
semantic stage -> revalidate + graph edges -> atomic pointer swap ->
registry record -> retire superseded snapshots (spec TSI-04 §5 order).

Subcommands: index, list, status, reindex, forget, install-semantic, watch.
"""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version as distribution_version
import contextlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import config
from jarvis import jobs
from jarvis import runtime
from jarvis.graph import (
    GraphStore,
    clear_graph_edges_for_repo,
    populate_graph_for_repo,
)
from jarvis.registry import (
    DEGRADED_STATUS,
    ORIGIN_FAILED_HARD,
    PARTIAL_STATUS,
    Registry,
    RegisteredRepo,
    SCIP_STATES,
    ScipStageFields,
    UNKNOWN_LANGUAGE,
    origin_of,
    recovery_for,
)
from jarvis.syntax_index import (
    SourceManifest,
    build_syntax_index,
    capture_sources,
    copy_syntax_tables,
    finalize_snapshot,
    validate_sources,
)
from jarvis.watch import Debouncer, should_ignore_path

if TYPE_CHECKING:
    from jarvis.semantic import SemanticIndexReport, SemanticWork

    from tree_sitter import Tree

if TYPE_CHECKING:
    from jarvis.semantic import SemanticIndexReport

_LANGUAGE_INDEXERS: dict[str, tuple[str, list[str]]] = {
    ".ts": ("typescript", ["scip-typescript", "index"]),
    ".tsx": ("typescript", ["scip-typescript", "index"]),
    ".py": ("python", ["scip-python", "index"]),
    ".java": ("java", ["scip-java", "index"]),
    ".kt": ("java", ["scip-java", "index"]),
    # No "index" token, unlike the others: scip-swift gained its `index`
    # subcommand only after v0.1.0 was released, so `scip-swift index …` fails
    # against that binary (it parses "index" as the repo path). The bare form
    # works on every version -- old binaries default the repo path to cwd, and
    # newer ones dispatch to `index` as their default subcommand. Verified
    # against both v0.1.0 and v0.1.1. Do not add "index" back.
    ".swift": ("swift", ["scip-swift"]),
}
# Priority order for tie-breaking when extension counts are equal.
_EXT_PRIORITY = [".ts", ".tsx", ".py", ".java", ".kt", ".swift"]

# Reverse of `_LANGUAGE_INDEXERS`, derived from it so the two cannot drift.
# Several extensions share a language (.ts/.tsx, .java/.kt) and map to the
# same command, so the collapse is lossless. Its keys are the valid
# `--language` values.
_INDEXER_BY_LANGUAGE: dict[str, list[str]] = {
    language: cmd for language, cmd in _LANGUAGE_INDEXERS.values()
}

_IGNORED_DIRS = config.IGNORED_DIRS

# `scip expt-convert` below this version cannot read scip.proto's `typed_range`
# oneof (its Go bindings predate occurrence_range.go), so it silently writes a
# schema-valid database with zero chunks and zero mentions -- every navigation
# query then returns empty. Verified: the same .scip file yields chunks=0/
# mentions=0 under v0.7.0 and chunks=1/mentions=14 under v0.9.0.
MIN_SCIP_VERSION = (0, 9, 0)
# scip-swift before 0.3.0 mis-dispatches xcodebuild for .xcodeproj repos
# (upstream fix 9bcf1688, first released in 0.3.0), silently producing
# broken indexes. Since 02-01, setup.sh auto-rolls installs to the latest
# release, so this runtime gate is the defense against a stale binary left
# earlier on PATH -- the same PATH-shadowing hazard the scip floor above
# guards against.
MIN_SCIP_SWIFT_VERSION = (0, 3, 0)

_BUNDLED_BINARY_REMEDY = "brew reinstall jarvis, then rerun indexing"
_OPTIONAL_INDEXER_REMEDIES = {
    "scip-python": "sh setup.sh --only scip-python",
    "scip-typescript": "sh setup.sh --only scip-typescript",
    "scip-java": "sh setup.sh --only scip-java",
    "scip-swift": "sh setup.sh --only scip-swift",
}


# PARTIAL_STATUS and UNKNOWN_LANGUAGE now live in registry.py (imported
# above): the status vocabulary is the registry's contract, and the CLI
# writer and the MCP reader must spell them identically.
#
# The search-only signature machinery was removed with the fallback design
# (spec §12: FALL-01/FALL-02 superseded). Every SCIP build/conversion
# failure now degrades the optional enrichment to exit-0 `degraded` with
# the cause recorded on the stage fields — the syntax baseline publishes
# regardless — and the watch suppression predicate owns the anti-treadmill
# policy that used to need signatures.
#
# The bash-shim remedy below is the retained piece: the cause text it wraps
# is genuinely the fix, and it now rides the recorded stage failure instead
# of a hard failure.
_BASH_SHIM_TOKENS = ("LAUNCHER_ARGS[@]", "unbound variable")

_BASH_SHIM_REMEDY = (
    "scip-java's generated javac wrapper requires bash >= 4.4, but this machine's "
    "default bash is older (macOS ships 3.2). Install a newer bash "
    "(`brew install bash`), run `sh setup.sh --only bash-shim`, then reindex."
)


def _bash_shim_failure(output: str) -> bool:
    """True when the indexer died on bash < 4.4 expanding an empty array."""
    return all(token in output for token in _BASH_SHIM_TOKENS)


class UnsupportedLanguageError(Exception):
    """Raised when no supported source extension is found under a repo."""


class IndexingError(Exception):
    """Raised when an indexing pipeline step (indexer/convert/zoekt) fails."""


class MissingBinaryError(IndexingError):
    """A pipeline step's executable was not on PATH.

    Deliberately its own type beside IndexingError (narrowed FALL-04, spec
    §12): a missing binary is a dependency-installation problem. For the
    REQUIRED stages (zoekt, grammars) it stays a loud hard failure; inside the optional
    SCIP stage it classifies the attempt as `unavailable` — exit-0
    degraded with the remedy recorded, never a fabricated SCIP snapshot.
    IS-A IndexingError, so every existing except-site keeps working.
    """


class ScipStageSkipped(Exception):
    """Internal marker: the watch suppression predicate decided this run
    skips the SCIP attempt (spec TSI-07, replacing FALL-05's whole-run
    skip). Never escapes index_repo — the baseline still runs, publishes,
    and preserves the last stage failure record."""


class NotAGitRepositoryError(Exception):
    """Raised when a repo path is not a git working tree.

    Indexing already required git -- `_git_head()` reads the commit SHA --
    so this is not a new restriction, just an early and explicit one.
    """


def detect_language(repo_path: Path) -> tuple[str, list[str]]:
    """Scan `repo_path`'s git-tracked files for supported source
    extensions; return `(language, indexer_command)` for whichever
    extension has the most files, ties broken by `_EXT_PRIORITY` order.

    Git-tracked, not a filesystem walk: a walk also counts gitignored
    vendored checkouts and sibling clones, which can outnumber the repo's
    own code and pick a language the repo does not use.

    `_IGNORED_DIRS` is still applied on top, because git does not exclude
    build output a repo happens to commit (a checked-in `dist/` or a
    vendored `node_modules`).
    """
    counts: Counter[str] = Counter()
    for name in _git_tracked_files(repo_path):
        path = Path(name)
        if any(part in _IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix in _LANGUAGE_INDEXERS:
            counts[path.suffix] += 1

    present = [ext for ext in _EXT_PRIORITY if counts[ext] > 0]
    if not present:
        raise UnsupportedLanguageError(
            f"no supported source files (.ts/.tsx/.py/.java/.kt/.swift) tracked under {repo_path}"
        )
    best_ext = max(present, key=lambda ext: (counts[ext], -_EXT_PRIORITY.index(ext)))
    return _LANGUAGE_INDEXERS[best_ext]


def _prefers_xcodebuild(repo_path: Path) -> bool:
    """True when `repo_path` has a checked-in `.xcodeproj`/`.xcworkspace`
    alongside `Package.swift`. `scip-swift`'s own `BuildBackendDetector`
    picks `swiftpm` whenever `Package.swift` exists, even when that can't
    build — e.g. a UIKit-only iOS package with no macOS platform support,
    where plain `swift build` fails with "no such module 'UIKit'" on the
    macOS host destination it defaults to."""
    return any(repo_path.glob("*.xcodeproj")) or any(repo_path.glob("*.xcworkspace"))


def _swift_indexer_cmd(
    base_cmd: list[str], repo_path: Path, scheme: str | None, cache_dir: Path
) -> list[str]:
    """Extend `base_cmd` (`["scip-swift"]`) with `--build-tool xcodebuild`
    (and `--scheme`, if given) when `repo_path` has a checked-in Xcode
    project — see `_prefers_xcodebuild`. Non-Swift callers never reach
    this function.

    `--cache-dir` rides every invocation on both build paths (D-05): it
    keeps the incremental cache, build scratch, and derived data out of
    the repo tree (and out of ~/Library/Developer/Xcode/DerivedData),
    under a per-repo directory whose lifecycle jarvis owns."""
    if not _prefers_xcodebuild(repo_path):
        return [*base_cmd, "--cache-dir", str(cache_dir)]
    cmd = [*base_cmd, "--build-tool", "xcodebuild"]
    if scheme:
        cmd += ["--scheme", scheme]
    return cmd + ["--cache-dir", str(cache_dir)]


def _java_indexer_env() -> dict[str, str]:
    """scip-java's Gradle plugin races against itself when Gradle runs tasks in
    parallel: two modules' `scipPrintDependencies` mutate shared state and the
    build dies with java.util.ConcurrentModificationException. Forcing
    single-threaded execution avoids it.

    Appended to any existing GRADLE_OPTS rather than replacing it, so a user's
    heap settings survive. Reported upstream.

    PATH gets the shim dir prepended when it holds a bash: scip-java's
    generated javac wrapper is `#!/usr/bin/env bash` (so bash comes from PATH)
    with `set -eu` and an unguarded `"${LAUNCHER_ARGS[@]}"`, which is an error on
    bash < 4.4. macOS ships 3.2, so every Maven build fails at
    maven-compiler-plugin's version probe without this. Remove once the pinned
    scip-java emits a bash-3.2-safe wrapper.

    Only the shim dir, never a general bin dir: prepending e.g. Homebrew's bin
    would also shadow java/mvn/git for the build.
    """
    existing = os.environ.get("GRADLE_OPTS", "")
    env = {"GRADLE_OPTS": f"{existing} -Dorg.gradle.parallel=false".strip()}
    shims = config.shim_dir()
    if (shims / "bash").exists():
        env["PATH"] = f"{shims}{os.pathsep}{os.environ.get('PATH', '')}"
    return env


def _git_tracked_files(repo_path: Path) -> list[str]:
    """Repo-relative paths of git-tracked files.

    Git is the source of truth for "what belongs to this repo". A
    filesystem walk also counts gitignored scratch directories -- vendored
    checkouts, sibling clones, worktrees -- which can outnumber the repo's
    own code and flip language detection to a language the repo does not
    actually use.

    `-z` (NUL-delimited) is required, not stylistic: with the default
    newline separator git quotes non-ASCII names, which would corrupt
    suffix parsing downstream.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), "ls-files", "-z"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise NotAGitRepositoryError(
            f"{repo_path} is not a git repository (git ls-files: {result.stderr.strip()})"
        )
    return [name for name in result.stdout.split("\0") if name]


_GITLINK_MODE = "160000"


def _tracked_blob_count(repo_path: Path) -> int:
    """How many git-tracked blobs exist at HEAD — the number of files
    `zoekt-git-index` should index, and so the expected search coverage.

    Not built on `_git_tracked_files`: that uses plain `ls-files -z`, which
    emits paths with no mode, and a submodule gitlink is indistinguishable
    from a file in that output. `-s` prefixes each entry with
    `<mode> <sha> <stage>\\t`, letting mode 160000 (gitlink) be dropped —
    required because `-submodules=false` means zoekt never descends into a
    submodule, so counting its gitlink would make the expectation unreachable.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), "ls-files", "-s", "-z"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise NotAGitRepositoryError(
            f"{repo_path} is not a git repository (git ls-files -s: {result.stderr.strip()})"
        )
    count = 0
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if not entry.startswith(f"{_GITLINK_MODE} "):
            count += 1
    return count


def ensure_git_repo(repo_path: Path) -> None:
    """Raise `NotAGitRepositoryError` unless `repo_path` is inside a git work
    tree. Extracted from `_git_head` so a caller that only needs the check --
    the MCP `indexRepo` pre-flight -- does not also need a commit to exist."""
    check = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        raise NotAGitRepositoryError(
            f"{repo_path} is not a git repository "
            f"(git rev-parse --is-inside-work-tree: {check.stderr.strip()})"
        )


def _git_head(repo_path: Path) -> str:
    """Current commit SHA.

    Distinguishes "not a git repository at all" from "git repository with
    no commits": `git rev-parse HEAD` fails identically in both cases, so
    a directory that isn't a git repo at all would otherwise be
    misdiagnosed as "has no commits yet". Checking `--is-inside-work-tree`
    first raises `NotAGitRepositoryError` for the former; only a real
    git repo with no commits reaches the `IndexingError` below, naming the
    cause rather than letting a bare CalledProcessError escape."""
    ensure_git_repo(repo_path)
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise IndexingError(
            f"{repo_path} has no commits yet (git rev-parse HEAD: {result.stderr.strip()})"
        )
    return result.stdout.strip()


def _scip_suppressed(
    entry: RegisteredRepo | None, *, watch: bool, scip_enabled: bool,
    explicit_scip: bool | None, language: str | None, scheme: str | None,
    head: str,
) -> bool:
    """The per-stage watch predicate (spec TSI-07), replacing FALL-05's
    whole-run skip. True skips ONLY the SCIP attempt; every debounced
    watch event still runs the baseline, the optional semantic stage, and
    publication. All four conditions must hold:

    1. The caller is watch — an explicit index/reindex always retries.
    2. SCIP is enabled and the selected language has a SCIP indexer.
    3. The persisted stage state is failed/unavailable and
       `scip_failed_at_sha` equals the current HEAD — the exact attempt
       that already failed. (FALL-03, retained by spec §12: the failure
       never changed the user's enablement, so an unchanged commit does
       not retry a known-failing compiler stage, but any new commit does.)
    4. No explicit `--scip` re-enable and no explicit `--language`/
       `--scheme` value that differs from the persisted override — either
       would invalidate the record this suppression is based on.

    A suppressed attempt never persists `scip_enabled=False`: suppression
    is an action on a prior failure, not a new capability state.
    Kept pure (no subprocess, no Registry) so the decision matrix is
    unit-testable without the observer machinery."""
    if not watch:  # (1)
        return False
    if entry is None:
        return False
    if not scip_enabled:  # (2a)
        return False
    if entry.language not in _INDEXER_BY_LANGUAGE:  # (2b)
        return False
    if entry.scip_state not in ("failed", "unavailable"):  # (3a)
        return False
    if entry.scip_failed_at_sha != head:  # (3b)
        return False
    if explicit_scip is True:  # (4a) explicit re-enable
        return False
    if language is not None and language != entry.language_override:  # (4b)
        return False
    if scheme is not None and scheme != entry.scheme_override:  # (4c)
        return False
    return True


def _resolve_scip_enabled(registry: Registry, slug: str, cli: bool | None) -> bool:
    """Resolve the reversible SCIP choice (spec TSI-07): an explicit
    `--scip`/`--no-scip` wins; omitted means the persisted choice, defaulting
    to enabled for a repo with no row yet. There is no environment tier —
    JARVIS_FALLBACK_SEARCH_ONLY was removed with the fallback design
    (spec §12)."""
    if cli is not None:
        return cli
    existing = registry.get(slug)
    if existing is not None:
        return existing.scip_enabled
    return True


def _run(cmd: list[str], *, cwd: Path, step: str,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """`env`, when given, is merged OVER a copy of `os.environ` rather than
    replacing it — a bare replacement would drop PATH and break the very
    subprocess lookup that finds the indexer.

    Returns the completed process so callers can read output on success;
    `zoekt-git-index` reports its file count on stderr, which the search
    coverage check parses.

    A missing executable raises `FileNotFoundError`, not a non-zero exit, so
    it is translated into an `IndexingError` with a binary-specific remedy:
    bundled binaries are reinstalled through Homebrew, while retained language
    indexers use their setup.sh selector.
    """
    merged = {**os.environ, **env} if env else None
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=merged)
    except FileNotFoundError as exc:
        binary = cmd[0]
        if binary in {"scip", "zoekt-git-index", "zoekt-webserver"}:
            remedy = _BUNDLED_BINARY_REMEDY
        else:
            remedy = _OPTIONAL_INDEXER_REMEDIES.get(
                binary, f"install {binary}, then rerun indexing"
            )
        raise MissingBinaryError(
            f"{step} failed: {binary} not found on PATH — {remedy}"
        ) from exc
    if result.returncode != 0:
        raise IndexingError(f"{step} failed ({' '.join(cmd)}):\n{result.stdout}\n{result.stderr}")
    return result


def _publish_atomically(target_dir: Path, versioned_name: str) -> None:
    """Write the pointer file via write-temp-then-rename (atomic on the
    same filesystem, POSIX `rename(2)`) so a concurrent reader never
    observes a half-written pointer — it either sees the old versioned
    filename or the new one, never a partial write.

    Pointer flip ONLY: retiring the superseded versioned artifacts moved
    to `_retire_superseded_snapshots`, which runs strictly AFTER the final
    registry write (spec TSI-04 stage 7). Deleting the old files here —
    the old behavior — meant a crash between this flip and the registry
    success left the new snapshot live and recorded, but the old
    generation already gone: an orphaned cleanup that could never be
    retried or audited."""
    pointer_file = target_dir / "current"
    tmp_pointer = target_dir / f".current.tmp-{os.getpid()}"
    tmp_pointer.write_text(versioned_name, encoding="utf-8")
    os.replace(tmp_pointer, pointer_file)


def _metadata_name(db_filename: str) -> str:
    """The metadata sibling of a snapshot database, derived from the
    actual filename (spec TSI-04): `.db` -> `.metadata.json`. The old
    writer hardcoded a commit-only name, which broke the moment filenames
    gained a generation suffix — both the writer and the cleanup must
    derive the sibling from the file they were given."""
    return db_filename.removesuffix(".db") + ".metadata.json"


def _retire_superseded_snapshots(slug: str, root: Path | None, keep: str) -> list[Path]:
    """Delete every versioned snapshot for `slug` except `keep` (the live
    pointer's target), together with each one's metadata sibling derived
    from the actual filename. Called strictly after the final registry
    write: a failure here must never fail the run (the new snapshot is
    published and recorded) — callers warn and continue, and the next
    successful run simply cleans again.

    Also sweeps the pre-baseline naming scheme (`index-<sha>.db` without a
    generation), which is what makes a first baseline reindex tidy up
    after the old writer."""
    target_dir = config.index_dir(slug, root)
    if not target_dir.is_dir():
        return []
    removed: list[Path] = []
    for db_file in sorted(target_dir.glob("index-*.db")):
        if db_file.name == keep:
            continue
        db_file.unlink(missing_ok=True)
        (target_dir / _metadata_name(db_file.name)).unlink(missing_ok=True)
        removed.append(db_file)
    return removed


def parse_scip_version(output: str) -> tuple[int, int, int] | None:
    """Parse `scip --version` output, e.g. "scip version v0.9.0".

    Returns None when the format is unrecognized, so an unexpected build
    string degrades to "cannot verify" rather than blocking indexing.
    """
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", output)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _scip_version_output() -> str:
    """Isolated for tests to monkeypatch."""
    try:
        result = subprocess.run(["scip", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise IndexingError(
            "scip not found on PATH — "
            "brew reinstall jarvis, then retry indexing"
        ) from exc
    return f"{result.stdout}\n{result.stderr}"


def _scip_swift_version_output() -> str:
    """Isolated for tests to monkeypatch."""
    try:
        result = subprocess.run(["scip-swift", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise IndexingError(
            "scip-swift not found on PATH — sh setup.sh --only scip-swift"
        ) from exc
    return f"{result.stdout}\n{result.stderr}"


def check_scip_swift_version() -> None:
    """Raise IndexingError when `scip-swift` is too old for the argv contract.

    Reuses parse_scip_version verbatim -- its v-optional regex already
    parses scip-swift's output, which prints `0.3.0 (swift 6.2.4)` with
    no `v` prefix.
    """
    version = parse_scip_version(_scip_swift_version_output())
    if version is None:
        # Unknown format: warn-by-omission rather than block, exactly as
        # check_scip_version does -- a wrong guess here would make Swift
        # indexing impossible against a valid future build.
        return
    if version < MIN_SCIP_SWIFT_VERSION:
        current = ".".join(str(p) for p in version)
        required = ".".join(str(p) for p in MIN_SCIP_SWIFT_VERSION)
        raise IndexingError(
            f"scip-swift v{current} is too old (need >= v{required}): versions before "
            "0.3.0 dispatch xcodebuild incorrectly for .xcodeproj repos and produce "
            "broken indexes. Run `sh setup.sh --only scip-swift`, and remove any older "
            "scip-swift earlier on PATH."
        )


def check_scip_version() -> None:
    """Raise IndexingError when `scip` is too old to preserve ranges."""
    version = parse_scip_version(_scip_version_output())
    if version is None:
        # Unknown format: warn-by-omission rather than block. A wrong guess
        # here would make indexing impossible against a valid future build.
        return
    if version < MIN_SCIP_VERSION:
        current = ".".join(str(p) for p in version)
        required = ".".join(str(p) for p in MIN_SCIP_VERSION)
        raise IndexingError(
            f"scip v{current} is too old (need >= v{required}): it cannot read scip.proto's "
            "typed_range oneof, so occurrence positions are dropped and navigation returns "
            "empty results. Run `brew reinstall jarvis`, then `jarvis reindex <slug>`, "
            "and remove any older scip earlier on PATH."
        )


def index_has_navigation_data(conn: sqlite3.Connection) -> bool:
    """True when the index carries positional data, not just symbols.

    `chunks` holds the occurrence blobs and `mentions` the symbol/role rows
    that every per-file nav tool reads. Both empty while `global_symbols` is
    populated means positions were dropped somewhere upstream -- the index
    looks healthy and answers every nav query with an empty list.
    """
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    mentions = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    return chunks > 0 and mentions > 0


def _zoekt_index_cmd(zoekt_dir: Path, repo_path: Path) -> list[str]:
    """The `zoekt-git-index` invocation shared by both publish paths.

    `zoekt-git-index`, not `zoekt-index`: it walks the git tree and reads
    blobs by SHA, so gitignored content — `.venv/`, `node_modules/`,
    vendored checkouts — is absent by construction rather than by a
    hand-maintained denylist. This is upstream's recommended tool for local
    git repos, and the same reasoning `detect_language()` already applies:
    read git, not the filesystem.

    Consequence: search reflects HEAD, while SCIP navigation reflects the
    working tree. Uncommitted edits are searchable only after a commit.

    `-incremental=false`: the default (true) skips indexing when the shard is
    newer than refs, which would refuse to repair an already-published
    incomplete shard. jarvis's registry owns the when-to-reindex decision.

    `-submodules=false`: submodules are indexed under their own slugs, so
    including them here would duplicate content across two indexes and make
    the coverage expectation from `_tracked_blob_count` unreachable.
    """
    return [
        "zoekt-git-index",
        "-index", str(zoekt_dir),
        "-incremental=false",
        "-submodules=false",
        str(repo_path),
    ]


_INDEXED_FILE_COUNT_RE = re.compile(r"attempting to index (\d+) total files")


def _parse_indexed_file_count(output: str) -> int | None:
    """How many files `zoekt-git-index` reported indexing, or None when the
    line is absent.

    Returns None rather than raising so an upstream log-format change
    degrades the coverage check to "unknown" instead of failing an otherwise
    healthy publish. The authoritative post-index count comes from zoekt's
    own `/api/list` at status time; this is the cheap index-time signal.
    """
    match = _INDEXED_FILE_COUNT_RE.search(output)
    return int(match.group(1)) if match else None


def _warn_on_coverage_shortfall(slug: str, expected: int, output: str) -> None:
    """Warn when the indexer saw fewer files than git tracks.

    Warns rather than failing: legitimate causes exist — zoekt skips files
    over its 2 MB `-file_limit`, files exceeding `-max_trigram_count`, and
    binaries. Mirrors how `index_has_navigation_data()` publishes a degraded
    index with a warning instead of refusing.
    """
    indexed = _parse_indexed_file_count(output)
    if indexed is None or indexed >= expected:
        return
    print(
        f"warning: {slug} indexed {indexed} of {expected} git-tracked files — "
        "searchCode results will be incomplete. Large files (>2MB) and binaries "
        "are skipped by design; a larger gap suggests a problem.",
        file=sys.stderr,
    )


def _sweep_zoekt_tmp_orphans(slug: str, root: Path | None = None) -> list[Path]:
    """Delete stranded `.tmp` shards for `slug`.

    `zoekt-git-index` writes `<name>.<n>.tmp` and renames on success, so a
    killed run (Ctrl-C, OOM) strands a temp file that is never usable and was
    never cleaned up — 545 MB of them accumulated once. A successful index is
    the natural moment to sweep this repo's leftovers.

    Slug-scoped like `_remove_zoekt_shards`: the `_v` in the glob stops "api"
    from matching "api-gateway"'s files.

    Runs between a successful `zoekt-git-index` run and `_publish_atomically`,
    so any failure here must never propagate: an `EACCES`/`EBUSY`/`EPERM` on
    `unlink()` would otherwise bubble up to `index_repo()`'s outer handler and
    discard a fully-successful publish over a cleanup-step failure.
    """
    zoekt_dir = config.data_dir(root) / ".zoekt"
    if not zoekt_dir.is_dir():
        return []
    removed: list[Path] = []
    for tmp in sorted(zoekt_dir.glob(f"{slug}_v*.zoekt*.tmp")):
        with contextlib.suppress(OSError):
            tmp.unlink()
            removed.append(tmp)
    return removed


def _resolve_scheme(registry: Registry, slug: str, scheme: str | None) -> str | None:
    """`scheme=None` means "leave the persisted override alone" (e.g. a
    `jarvis watch` reindex, which never repeats `--scheme`) rather than
    "clear it" -- looks up the existing registry row and falls back to its
    `scheme_override` when the caller passed nothing explicit."""
    if scheme is not None:
        return scheme
    existing = registry.get(slug)
    return existing.scheme_override if existing is not None else None


def _resolve_language(registry: Registry, slug: str, language: str | None) -> str | None:
    """`language=None` means "leave the persisted override alone" (a
    `jarvis watch` reindex never repeats the flag) rather than "clear
    it" — the same contract as `_resolve_scheme`. Returning None means no
    override is in force and detection should run."""
    if language is not None:
        return language
    existing = registry.get(slug)
    return existing.language_override if existing is not None else None



def _resolve_semantic_include(
    registry: Registry, slug: str, include: tuple[str, ...] | None
) -> tuple[str, ...]:
    """`include=None` means "leave the persisted value alone" (a `jarvis
    watch` reindex never repeats the flag) rather than "clear it" — the
    same contract as `_resolve_scheme`."""
    if include is not None:
        return include
    existing = registry.get(slug)
    return existing.semantic_include if existing is not None else ()


def _print_semantic_report(report: "SemanticIndexReport") -> None:
    """Index-time semantic summary. Everything goes to stderr, consistent
    with the other semantic notices, so stdout stays just `indexed <slug>`
    for scripting."""
    print(f"semantic: {report.rows} chunks from {report.files} files", file=sys.stderr)
    for skipped in report.skipped:
        print(f"semantic: skipped {skipped.file_path} ({skipped.reason})", file=sys.stderr)
    if report.token_stats is not None:
        stats = report.token_stats
        print(f"semantic: chunk tokens p50={stats.p50} p90={stats.p90} "
              f"max={stats.max}", file=sys.stderr)
    if report.prefix_warning:
        print(f"warning: {report.prefix_warning}", file=sys.stderr)
    if report.truncated is None:
        print("semantic: could not measure truncation", file=sys.stderr)
    elif report.truncated > 0:
        print(
            f"warning: {report.truncated} chunks exceeded the model's token limit "
            "and were truncated",
            file=sys.stderr,
        )

def _semantic_extra_missing() -> bool:
    """Isolated for tests to monkeypatch."""
    # The same top-level modules the semantic stage itself imports
    # lazily: semantic.py's `import lancedb` and embeddings.py's
    # `from sentence_transformers import SentenceTransformer`.
    # tree_sitter_language_pack is deliberately excluded — chunker.py
    # falls back to fixed-window chunking on any failure, so it is not
    # a hard requirement for semantic search.
    return (
        importlib.util.find_spec("lancedb") is None
        or importlib.util.find_spec("sentence_transformers") is None
    )


def _at_interactive_tty() -> bool:
    """Isolated for tests to monkeypatch."""
    # pip's convention: either stream redirected means automation, and
    # automation must never block on stdin — the non-TTY defense behind
    # the structural offer_semantic gate (SEMA-02).
    return sys.stdin.isatty() and sys.stdout.isatty()


def _install_semantic_extra() -> bool:
    """Isolated for tests to monkeypatch."""
    # The locked install command as a fixed argv list — never a shell
    # string, never built from the prompt answer. Deliberately not _run:
    # an install failure must warn and continue, not raise. torch-scale
    # downloads can take minutes, so a stalled network is bounded at
    # 600s (the index is already published; only the offer waits).
    uv = shutil.which("uv")
    if uv is None:
        return False
    try:
        result = subprocess.run(
            [uv, "pip", "install", "--python", sys.executable, "jarvis-mcp[semantic]"],
            capture_output=True, text=True, errors="replace", timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        # Every way the spawn or its decode can fail lands here, not just
        # the timeout: an OSError when the resolved uv cannot exec (broken
        # interpreter after `which` said yes, TOCTOU unlink, EACCES), or a
        # decode failure if uv/pip ever emit non-UTF-8 bytes that even
        # errors="replace" lets through. The index is already published —
        # the caller's contract is one stderr warning and rc 0.
        return False
    return result.returncode == 0


def _semantic_project_root() -> Path:
    """Return the checkout that owns this source-build CLI."""
    return Path(__file__).resolve().parents[2]


def _cmd_install_semantic(args: argparse.Namespace) -> int:
    """Prepare semantic dependencies in a source checkout without shell work."""
    del args
    if runtime.is_frozen():
        print(
            "semantic support is not installable — the Homebrew binary "
            "distribution excludes semantic dependencies",
            file=sys.stderr,
        )
        return 1

    project_root = _semantic_project_root()
    if not (project_root / "pyproject.toml").is_file():
        print(
            "semantic support installation requires a jarvis source checkout "
            f"(missing {project_root / 'pyproject.toml'})",
            file=sys.stderr,
        )
        return 1

    if not _semantic_extra_missing():
        print("semantic support already installed")
        return 0

    uv = shutil.which("uv")
    if uv is None:
        print("semantic support installation requires uv", file=sys.stderr)
        return 1

    print("installing semantic support...", file=sys.stderr)
    try:
        result = subprocess.run(
            [uv, "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"],
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        print("semantic support installation failed", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print("semantic support installation failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return 1

    importlib.invalidate_caches()
    if _semantic_extra_missing():
        print(
            "semantic support installation completed but lancedb or "
            "sentence_transformers is unavailable",
            file=sys.stderr,
        )
        return 1

    print("semantic support installed")
    return 0


def _prepare_semantic_stage(
    repo_path: Path, slug: str, root: Path | None,
    include_prefixes: tuple[str, ...], manifest: SourceManifest | None,
) -> "SemanticWork | None":
    """Optional semantic stage, prepare half (spec TSI-04 stage 5 + TSI-09):
    identity/admission decisions only — no weights loaded, no bytes
    encoded. Runs BEFORE the syntax build so `semantic_parse_paths` can
    feed `build_syntax_index`'s shared-parse callback. Optional and
    non-fatal: a missing `semantic` extra returns None with a hint, any
    other failure warns and lets the baseline publish proceed."""
    if runtime.is_frozen():
        print(
            "semantic indexing skipped — semantic search is not included "
            "in the Homebrew binary distribution",
            file=sys.stderr,
        )
        return None
    try:
        from jarvis import semantic
    except ImportError:
        print(
            "semantic indexing skipped — install jarvis-mcp[semantic] "
            "(uv tool install), or `uv sync --extra semantic` in a source checkout",
            file=sys.stderr,
        )
        return None
    try:
        return semantic.prepare_semantic(
            repo_path, slug, root=root, include_prefixes=include_prefixes,
            manifest=manifest,
        )
    except Exception as exc:
        print(
            f"warning: semantic indexing failed (index still published): {exc}",
            file=sys.stderr,
        )
        return None


def _finish_semantic_stage(
    work: "SemanticWork", prepared_chunks: dict, pool: object,
) -> bool:
    """Optional semantic stage, finish half: chunk (reusing the shared
    parse trees the syntax build captured) + embed + publish the LanceDB
    table. Optional and non-fatal — the previous table (if any) stays live
    on any failure."""
    from jarvis import semantic
    from jarvis.embeddings import SemanticExtraMissingError

    # Task-4 review directive: `finish_semantic` silently falls back to
    # re-chunking a file when its prepared entry no longer matches the
    # admitted input's hash. The discard path must be observable — say so
    # at debug level before the fallback kicks in.
    for item in work.inputs:
        chunks = prepared_chunks.get(item.file_path)
        if chunks is not None and any(c.file_hash != item.file_hash for c in chunks):
            print(
                f"debug: semantic: discarding prepared chunks for "
                f"{item.file_path} — file hash changed between syntax capture "
                "and semantic finish; re-chunking from source",
                file=sys.stderr,
            )
    try:
        report = semantic.finish_semantic(work, prepared_chunks=prepared_chunks,
                                          pool=pool)
        _print_semantic_report(report)
    except SemanticExtraMissingError as exc:
        print(f"semantic indexing skipped — {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        print(
            f"warning: semantic indexing failed (index still published): {exc}",
            file=sys.stderr,
        )
        return False
    return True


def _run_semantic_stage(repo_path: Path, slug: str, root: Path | None,
                        include_prefixes: tuple[str, ...] = ()) -> bool:
    """Standalone prepare+finish in one call, for callers with no syntax
    capture to share a parse with (the interactive post-install path).
    Optional and non-fatal: a missing `semantic` extra skips with a hint,
    any other failure warns and lets the publish proceed."""
    work = _prepare_semantic_stage(repo_path, slug, root, include_prefixes, None)
    if work is None:
        return False
    from jarvis.syntax import ParserPool

    return _finish_semantic_stage(work, {}, ParserPool())


class ScipAttempt:
    """Outcome of the optional SCIP enrichment stage (spec TSI-04 stage 3).
    `state` is the registry stage vocabulary value: `available` when real
    converter output was produced at `db_path`; `failed` for a build/
    conversion failure; `unavailable` for absent/incompatible tooling.
    `reason` is the one-line carrier for status_reason and the watch
    diagnostic; `stderr` the complete output for the stage field."""

    __slots__ = ("state", "reason", "stderr", "db_path")

    def __init__(self, state: str, reason: str | None = None,
                 stderr: str | None = None, db_path: Path | None = None) -> None:
        self.state = state
        self.reason = reason
        self.stderr = stderr
        self.db_path = db_path


def _attempt_scip(
    repo_path: Path, language: str, scheme: str | None, slug: str,
    scratch: Path,
) -> ScipAttempt:
    """The optional SCIP enrichment boundary (spec TSI-04 stage 3). Every
    failure mode listed in the spec — absent executables, incompatible
    tool versions, language build failures, converter failure — returns a
    `failed`/`unavailable` ScipAttempt instead of raising: the baseline
    publishes regardless, and the run records exit-0 `degraded`
    (narrowed FALL-04, spec §12). Only unexpected Jarvis programming
    errors propagate."""
    try:
        check_scip_version()
        if language == "swift":
            check_scip_swift_version()
    except IndexingError as exc:
        # Absent or too-old tooling: `unavailable` — a setup problem, not
        # a build failure.
        return _attempt_from_exception(exc, "unavailable")

    indexer_cmd = list(_INDEXER_BY_LANGUAGE[language])
    if language == "swift":
        # Cache outside the repo tree keeps scip-swift's build products
        # out of the working copy and out of
        # ~/Library/Developer/Xcode/DerivedData.
        indexer_cmd = _swift_indexer_cmd(
            indexer_cmd, repo_path, scheme, config.swift_cache_dir(slug)
        )

    scip_path = scratch / "index.scip"
    db_path = scratch / "index.db"
    try:
        _run([*indexer_cmd, "--output", str(scip_path)], cwd=repo_path,
             step=f"{indexer_cmd[0]} index",
             env=_java_indexer_env() if language == "java" else None)
        _run(
            ["scip", "expt-convert", "--output", str(db_path), str(scip_path)],
            cwd=repo_path,
            step="scip expt-convert",
        )
    except MissingBinaryError as exc:
        return _attempt_from_exception(exc, "unavailable")
    except IndexingError as exc:
        text = str(exc)
        if language == "java" and _bash_shim_failure(text):
            # The remedy IS the fix; ride it on the recorded stage failure.
            return _attempt_from_text(f"{_BASH_SHIM_REMEDY}\n\n{text}", "failed")
        return _attempt_from_text(text, "failed")
    # Empty/invalid converter output is rejected as an indexing error,
    # not accepted as an available snapshot the reader would crash on
    # (spec TSI-04: known optional SCIP failures include empty/invalid
    # indexer output).
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] == 0:
        return _attempt_from_text(
            "scip expt-convert produced no SCIP documents table — "
            "the indexer output was empty or invalid",
            "failed",
        )
    return ScipAttempt(state="available", db_path=db_path)


def _attempt_from_exception(exc: Exception, state: str) -> ScipAttempt:
    return _attempt_from_text(str(exc), state, exc.__class__.__name__)


def _attempt_from_text(text: str, state: str, fallback_reason: str | None = None) -> ScipAttempt:
    """One-line classified reason (D-03) plus the complete text (D-02),
    truncation display-only — the stage fields persist unbounded."""
    reason = next(
        (line for line in text.splitlines() if line.strip()),
        fallback_reason or state,
    )
    return ScipAttempt(state=state, reason=reason, stderr=text)


def _open_previous_snapshot(slug: str, root: Path | None) -> sqlite3.Connection | None:
    """Read-only connection to the currently published navigation snapshot,
    for `build_syntax_index`'s incremental reuse; None when nothing is
    published. A legacy (pre-baseline) db opens fine — the builder simply
    finds no `syntax_files` table and extracts from scratch, never
    mutating the published file."""
    index_dir = config.index_dir(slug, root)
    pointer = index_dir / "current"
    if not pointer.is_file():
        return None
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name:
        return None
    db_path = index_dir / name
    if not db_path.is_file():
        return None
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _reject_duplicate_slug_for_path(registry: Registry, slug: str, repo_path: Path) -> None:
    """One slug per repo path.

    `_pin_zoekt_repo_name` re-pins `zoekt.name` before every single index run,
    so two slugs indexing the same path don't actually corrupt each other's
    shard name — each stays correctly pinned at the moment it runs. The rule
    exists for three other reasons instead: (a) duplicate disk usage from
    indexing near-identical content twice under two slugs, (b) ambiguity
    about which slug is "the" search index for that path, and (c) on a
    linked worktree, `git config` writes to the repo's *shared* config
    (see CLAUDE.md's `zoekt.name` caveat), so two slugs on two worktrees of
    the same repo could otherwise race to set it — this per-path check
    prevents the common single-worktree case.

    Compares resolved paths because rows written before this check existed
    may hold unresolved ones. Same slug at the same path is the normal
    reindex/watch case and passes.
    """
    for existing in registry.list():
        if existing.slug == slug:
            continue
        try:
            same = Path(existing.path).resolve() == repo_path
        except OSError:
            # A registered path that no longer exists cannot collide.
            continue
        if same:
            raise IndexingError(
                f"{repo_path} is already indexed as {existing.slug!r}. "
                f"One slug per repo — run `jarvis forget {existing.slug}` first, "
                f"or reindex that slug instead."
            )


def _reject_slug_bound_to_another_path(
    registry: Registry, slug: str, repo_path: Path,
) -> None:
    """One path per slug -- the inverse of `_reject_duplicate_slug_for_path`,
    which enforces one slug per path and deliberately skips this direction.

    Without it, `/a/app` and `/b/app` both derive the slug `app` and the
    second silently overwrites the first's published index. That mattered
    little while every index was a deliberate `jarvis index` invocation; the
    MCP `indexRepo` path supplies paths and never chooses slugs, so it fires
    routinely.

    A registered path that no longer exists is a MOVE, not a collision --
    `upsert` already does `path=excluded.path` -- so rejection requires both
    paths to resolve. Mirrors the sibling guard's `except OSError: continue`.
    """
    existing = registry.get(slug)
    if existing is None:
        return
    try:
        registered = Path(existing.path).resolve(strict=True)
    except OSError:
        return
    if registered == repo_path:
        return
    raise IndexingError(
        f"slug {slug!r} is already bound to {registered}, not {repo_path}. "
        f"Index this repo under a different name with "
        f"`jarvis index {repo_path} --slug <name>`, or run "
        f"`jarvis forget {slug}` first."
    )


def resolve_slug_for_path(registry: Registry, repo_path: Path) -> str:
    """The slug an index run for `repo_path` should use.

    Resolution order matters: a repo registered under an explicit `--slug`
    must keep it, so a path lookup precedes basename derivation. Only a
    genuinely unregistered path falls through to `config.repo_slug`.
    """
    resolved = repo_path.resolve()
    for entry in registry.list():
        try:
            if Path(entry.path).resolve() == resolved:
                return entry.slug
        except OSError:
            continue
    slug = config.repo_slug(resolved.name)
    _reject_slug_bound_to_another_path(registry, slug, resolved)
    return slug


def _record_failure_best_effort(
    registry: Registry, slug: str, repo_path: Path, language: str,
    reason: str, text: str,
) -> None:
    """`Registry.record_failure` demoted to a stderr warning when the write
    itself raises (locked registry.db, disk-full -- often the very condition
    that triggered the handler). The failure row is bookkeeping ABOUT a
    failure: losing it must never replace or swallow the error the handler
    is about to raise, so every caller still raises its original error
    right after this, converted or not (WR-03)."""
    try:
        registry.record_failure(slug, str(repo_path), language,
                                ORIGIN_FAILED_HARD, reason, text)
    except Exception as recexc:
        rec_reason = next(
            (line for line in str(recexc).splitlines() if line.strip()),
            recexc.__class__.__name__)
        print(
            f"warning: recording the failed run for {slug} also failed — "
            f"{rec_reason}; the original failure ({reason}) is still raised "
            "and reported, but the registry row was not updated.",
            file=sys.stderr,
        )


def index_repo(
    repo_path: Path, *, slug: str | None = None, root: Path | None = None,
    scheme: str | None = None, semantic_include: tuple[str, ...] | None = None,
    language: str | None = None, scip: bool | None = None,
    semantic: bool = True,
    watch: bool = False,
) -> str:
    """Runs the staged pipeline for one repo (spec TSI-04 §5 order) and
    returns the slug it was published under.

    Stages and failure boundaries:

    1. Validate git input, slug/path ownership, and persisted
       configuration. SCIP tooling is deliberately NOT validated here — it
       is optional enrichment (narrowed FALL-04, spec §12).
    2. Capture tracked input, extract or reuse syntax declarations, and
       build a scratch syntax snapshot.
    3. Optionally attempt SCIP when enabled and supported, subject only to
       the watch suppression predicate (`_scip_suppressed`). Any expected
       failure degrades to exit-0 `degraded`; the baseline still publishes.
    4. Build Zoekt — its failure fails the run (nothing publishes).
    5. Optional semantic stage with its existing warning/nonfatal behavior.
    6. `validate_sources` + graph edge update. Graph/SQLite failures are
       storage failures (hard), never permission to claim degradation. A
       generation without usable SCIP data clears every outgoing edge the
       repo's packages own, leaving package identities intact.
    7. Publish via the scratch build -> unique final names -> write-temp +
       `os.replace` pointer flip; then record the final registry state;
       then retire superseded snapshots — strictly in that order, so a
       crash can never orphan the cleanup or destroy a live snapshot.
       Registry failure after the pointer flip raises nonzero and reports
       that the navigation snapshot is already live; never a degradation.

    Registry status vocabulary (spec TSI-06): `indexing` while running,
    `indexed` (baseline complete, SCIP usable/disabled/unsupported),
    `partial` (published with documented syntax/SCIP extraction gaps),
    `degraded` (published but enabled SCIP failed/unavailable/suppressed),
    `failed` (required stage/storage/publication/bookkeeping failure).

    An explicit `language` (or one persisted from an earlier `--language`)
    selects only the SCIP enrichment language — an override means "do not
    guess", not "guess then correct" — and never restricts the syntax
    baseline, which classifies every tracked file by its own extension. A
    repo with no SCIP-indexable language still publishes its baseline with
    `scip_state="unsupported"` (spec TSI-07); an invalid PERSISTED
    override remains a user-input error.

    `scip=None` means "use the persisted choice, defaulting to enabled for
    a new repo"; an explicit value updates it (spec TSI-07). `watch=True`
    marks a debounced watch caller so the suppression predicate may skip
    only the SCIP stage; explicit index/reindex calls always retry.

    `semantic=False` skips the optional semantic stage even when the extra is
    installed -- the MCP `indexRepo` path passes it so an agent tool call can
    never implicitly download embedding weights.
    """
    repo_path = repo_path.resolve()
    slug = config.repo_slug(slug or repo_path.name)
    # Acquired before the duplicate guards and before the transitional
    # `indexing` upsert: exclusion has to cover registry writes and artifact
    # writes alike, or two writers race on the same slug. `flock` and not a
    # pidfile -- a pidfile is a discovery cache, and nothing here binds a port
    # to arbitrate a lost race the way ZoektLifecycle does.
    with jobs.build_lock(slug, root=root):
        return _index_repo_locked(
            repo_path, slug=slug, root=root, scheme=scheme,
            semantic_include=semantic_include, language=language,
            scip=scip, watch=watch, semantic=semantic,
        )


def _index_repo_locked(
    repo_path: Path, *, slug: str, root, scheme, semantic_include,
    language, scip, watch, semantic,
) -> str:
    """`index_repo`'s body, running under the per-slug build lock. Split out
    only so the lock's extent is visible in one place; the staged pipeline is
    unchanged."""
    from jarvis.syntax import ParserPool

    sha = _git_head(repo_path)

    registry = Registry(config.data_dir(root) / "registry.db")
    try:
        _reject_duplicate_slug_for_path(registry, slug, repo_path)
        _reject_slug_bound_to_another_path(registry, slug, repo_path)
    except Exception:
        registry.close()
        raise
    # After the duplicate-slug gate, not before: rejecting a request for a
    # repo path already registered under another slug shouldn't depend on
    # any SCIP tooling being installed -- there is no SCIP requirement at
    # all before the optional stage now (spec TSI-04 §5 stage 1).
    # D-05 (retained): every failure from here until the run's first upsert
    # predates any registry write, so without this wrap a hard failure
    # would leave no row at all -- nothing could explain it and `jarvis
    # reindex <slug>` would report "no such repo". `_git_head` above stays
    # outside deliberately: it runs before the Registry exists, and a repo
    # with no commits is a malformed request, not a failed index run.
    resolved_language: str | None = None
    # The predicate's condition 4 compares the EXPLICIT caller values --
    # a watch run that never passed --language/--scheme must not look
    # like a change just because resolution filled the effective values
    # in (spec TSI-07).
    explicit_language = language
    explicit_scheme = scheme
    try:
        language_override = _resolve_language(registry, slug, language)
        if language_override is not None:
            if language_override not in _INDEXER_BY_LANGUAGE:
                raise UnsupportedLanguageError(
                    f"{slug!r} has a persisted language override {language_override!r} that is no "
                    f"longer supported (expected one of {sorted(_INDEXER_BY_LANGUAGE)})"
                )
            language = language_override
        else:
            try:
                language, _ = detect_language(repo_path)
            except UnsupportedLanguageError:
                # No SCIP-indexable language: the syntax baseline still
                # covers every supported extension, so the run publishes
                # with scip_state="unsupported" instead of failing (spec
                # TSI-07). UNKNOWN_LANGUAGE persists because
                # registry.language is NOT NULL.
                language = UNKNOWN_LANGUAGE
        resolved_language = language
        scheme = _resolve_scheme(registry, slug, scheme)
        semantic_include = _resolve_semantic_include(registry, slug, semantic_include)
        scip_enabled = _resolve_scip_enabled(registry, slug, scip)
        existing = registry.get(slug)
        # Read BEFORE the transitional upsert: the predicate consults the
        # prior stage failure record, and `indexing` must not disturb it.
        suppressed = _scip_suppressed(
            existing, watch=watch, scip_enabled=scip_enabled,
            explicit_scip=scip, language=explicit_language,
            scheme=explicit_scheme, head=sha,
        )
    except Exception as exc:
        # `resolved_language` is None until resolution completes -- the
        # honest record for a run that died before establishing one (D-06:
        # the failed attempt's facts, never the last good run's).
        text = str(exc)
        reason = next((line for line in text.splitlines() if line.strip()),
                      exc.__class__.__name__)
        _record_failure_best_effort(
            registry, slug, repo_path,
            resolved_language if resolved_language is not None else UNKNOWN_LANGUAGE,
            reason, text)
        registry.close()
        raise

    scip_supported = resolved_language in _INDEXER_BY_LANGUAGE
    attempt_scip = scip_enabled and scip_supported and not suppressed

    # Transitional write: flips the row to `indexing` and persists the
    # resolved enablement, while `scip_stage=None` leaves any prior stage
    # failure record untouched for the decision below to read (spec
    # TSI-06). Like --scheme/--language, an explicit enablement choice is
    # persisted the moment the row exists, pre-pipeline failures included.
    registry.upsert(slug, str(repo_path), resolved_language, None, "indexing",
                    scheme_override=scheme, semantic_include=semantic_include,
                    language_override=language_override, scip_enabled=scip_enabled)

    # Set the moment `_publish_atomically` flips the pointer. Everything
    # after that is registry bookkeeping and cleanup, never a degradation
    # path (spec TSI-04 §5 stage 7; WR-02 retained).
    published = False
    versioned_name: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-index-") as scratch_dir:
            scratch = Path(scratch_dir)

            # -- Stage 2: capture + syntax baseline ----------------------
            manifest = capture_sources(repo_path, scratch)
            syntax_db = scratch / "syntax.db"
            previous = _open_previous_snapshot(slug, root)
            pool = ParserPool()
            prepared_chunks: dict = {}
            chunk_capture_error: Exception | None = None
            try:
                sem_work = (
                    _prepare_semantic_stage(repo_path, slug, root, semantic_include, manifest)
                    if semantic else None
                )
                parse_for = frozenset()
                if sem_work is not None:
                    from jarvis import semantic as _semantic
                    parse_for = _semantic.semantic_parse_paths(sem_work)

                def _collect_chunks(captured, data, tree) -> None:
                    # Shared-parse chunk capture for the semantic stage
                    # (spec TSI-09): chunker reuses this exact tree instead
                    # of reparsing. A stale entry is discarded by
                    # `_finish_semantic_stage`'s hash check, observably.
                    # Optional-consumer isolation (plan §"collect_chunks"
                    # contract): a failure here is a semantic-stage problem
                    # and must never abort the required syntax build --
                    # broad on purpose. Record the FIRST error and stop
                    # collecting; stage 5 then discards this run's semantic
                    # work (skipping `_finish_semantic_stage` entirely, so
                    # the previous LanceDB table stays live) and warns once
                    # at its stage.
                    nonlocal chunk_capture_error
                    from jarvis import chunker as _chunker

                    if chunk_capture_error is not None:
                        return
                    try:
                        prepared_chunks[captured.file_path] = _chunker.chunk_file(
                            captured.file_path,
                            data.decode("utf-8", errors="replace"),
                            captured.file_hash or "",
                            captured.language or "",
                            pool=pool, tree=tree,
                        )
                    except Exception as exc:
                        chunk_capture_error = exc

                build_report = build_syntax_index(
                    syntax_db, manifest, pool=pool, previous=previous,
                    parse_for=parse_for,
                    on_parsed=_collect_chunks if sem_work is not None else None,
                )
            finally:
                if previous is not None:
                    previous.close()
            # Unsupported extensions alone do not make a run partial (spec
            # TSI-06); a supported file that was size-skipped, undecodable,
            # or only partially parsed does.
            syntax_gap = bool(
                build_report.counts.failed
                or build_report.counts.partial
                or build_report.counts.skipped
            )

            # -- Stage 3: optional SCIP enrichment -----------------------
            attempt: ScipAttempt | None = None
            if attempt_scip:
                attempt = _attempt_scip(repo_path, resolved_language, scheme,
                                        slug, scratch)

            # -- Stage 4: Zoekt (required; failure fails the run) --------
            zoekt_dir = config.data_dir(root) / ".zoekt"
            zoekt_dir.mkdir(parents=True, exist_ok=True)
            _pin_zoekt_repo_name(repo_path, slug)
            tracked = _tracked_blob_count(repo_path)
            zoekt_result = _run(_zoekt_index_cmd(zoekt_dir, repo_path), cwd=repo_path,
                                step="zoekt-git-index")
            _warn_on_coverage_shortfall(slug, tracked, zoekt_result.stderr)
            _sweep_zoekt_tmp_orphans(slug, root)

            # -- Stage 5: optional semantic finish (nonfatal) ------------
            # A chunk-capture consumer failure discards this run's semantic
            # work entirely (plan :563): finish is never attempted, so the
            # previous LanceDB table stays live, and the stage warns once.
            semantic_ok = False
            if sem_work is not None:
                if chunk_capture_error is not None:
                    print(
                        "warning: semantic indexing failed (index still "
                        f"published): {chunk_capture_error}",
                        file=sys.stderr,
                    )
                else:
                    semantic_ok = _finish_semantic_stage(
                        sem_work, prepared_chunks, pool)

            # -- Stage 6: revalidate sources + graph edges ---------------
            # Storage failures here are HARD failures (spec TSI-04: graph/
            # SQLite failures are never permission to claim degradation),
            # and the work runs before navigation publication (WR-02).
            validate_sources(repo_path, manifest)

            generation = uuid.uuid4().hex
            versioned_name = f"index-{sha}-{generation}.db"
            published_at = datetime.now(UTC).isoformat()

            if attempt is not None and attempt.db_path is not None:
                # Genuine converter-produced SCIP tables, preserved
                # unchanged alongside the Jarvis-owned syntax rows. Old
                # SCIP data is never copied forward into a generation that
                # has none.
                final_db = attempt.db_path
                conn = sqlite3.connect(final_db)
                try:
                    copy_syntax_tables(syntax_db, conn)
                    facts = finalize_snapshot(
                        conn, generation=generation, commit_sha=sha,
                        published_at=published_at,
                        source_hash=manifest.source_hash,
                        scip_state=attempt.state,
                    )
                    has_nav = index_has_navigation_data(conn)
                finally:
                    conn.close()
            else:
                final_db = syntax_db
                if attempt is not None:
                    # The attempt failed/unavailable: the singleton records
                    # why this generation carries no SCIP data.
                    singleton_state = attempt.state
                elif not scip_enabled:
                    singleton_state = "disabled"
                elif suppressed and existing is not None and existing.scip_state:
                    # Suppressed watch run: mirror the preserved stage
                    # state — it is why this generation has no SCIP data.
                    singleton_state = existing.scip_state
                else:
                    singleton_state = "unsupported"
                conn = sqlite3.connect(final_db)
                try:
                    facts = finalize_snapshot(
                        conn, generation=generation, commit_sha=sha,
                        published_at=published_at,
                        source_hash=manifest.source_hash,
                        scip_state=singleton_state,
                    )
                finally:
                    conn.close()
                has_nav = False

            graph_store = GraphStore(config.data_dir(root) / "registry.db")
            try:
                if attempt is not None and attempt.db_path is not None:
                    index_conn = sqlite3.connect(final_db)
                    try:
                        populate_graph_for_repo(graph_store, slug, index_conn)
                    finally:
                        index_conn.close()
                else:
                    # SCIP-less generation: rebuild-not-accumulate means the
                    # repo's own outgoing edges are cleared, but package
                    # identities other repos resolve against survive, and
                    # nothing is synthesized from Tree-sitter (spec TSI-04).
                    clear_graph_edges_for_repo(graph_store, slug)
            finally:
                graph_store.close()

            # -- Stage 7: publish (scratch build -> unique names -> flip) -
            target_dir = config.index_dir(slug, root)
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(final_db, target_dir / versioned_name)
            (target_dir / _metadata_name(versioned_name)).write_text(
                json.dumps({
                    "project": config.PROJECT,
                    "repo": slug,
                    "branch": config.BRANCH,
                    "commit_sha": sha,
                    "published_at": published_at,
                    "generation": generation,
                    "source_hash": manifest.source_hash,
                }),
                encoding="utf-8",
            )
            _publish_atomically(target_dir, versioned_name)
            published = True

        # Terminal stage decision (spec TSI-06 vocabulary). An explicit
        # ScipStageFields is the ONLY writer of the stage columns, so a
        # successful baseline bookkeeping can never erase an active SCIP
        # failure (narrowed D-04): successful enrichment clears its own
        # fields; a suppressed run passes None and preserves them.
        if attempt is not None and attempt.state == "available":
            stage = ScipStageFields(state="available")
        elif attempt is not None:
            stage = ScipStageFields(
                state=attempt.state, failure_reason=attempt.reason,
                failure_stderr=attempt.stderr, failed_at_sha=sha,
            )
        elif not scip_enabled:
            stage = ScipStageFields(state="disabled")
        elif not scip_supported:
            stage = ScipStageFields(state="unsupported")
        else:
            # Suppressed: preserve the last failure fields and state.
            stage = None

        if attempt is not None and attempt.state in ("failed", "unavailable"):
            final_status = DEGRADED_STATUS
        elif suppressed:
            final_status = DEGRADED_STATUS
        elif syntax_gap or (attempt is not None and not has_nav):
            final_status = PARTIAL_STATUS
        else:
            final_status = "indexed"

        # Exit-0 degraded mirrors the concise stage cause into
        # status_reason so list/status/last_index_run stay informative;
        # full SCIP stderr rides the stage field (spec TSI-06). Hard run
        # failures describe themselves through record_failure instead.
        degraded_reason = None
        if final_status == DEGRADED_STATUS:
            if attempt is not None:
                degraded_reason = attempt.reason
            elif existing is not None:
                degraded_reason = existing.scip_failure_reason

        registry.upsert(slug, str(repo_path), resolved_language, sha, final_status,
                        scheme_override=scheme, semantic_include=semantic_include,
                        language_override=language_override,
                        status_reason=degraded_reason,
                        scip_enabled=scip_enabled, scip_stage=stage)
        registry.mark_tracked_files(slug, tracked)
        if semantic_ok:
            registry.mark_semantic_indexed(slug)

        if suppressed and existing is not None:
            print(
                f"note: {slug}: SCIP skipped — the previous attempt "
                f"({existing.scip_state}) failed at this commit "
                f"({existing.scip_failure_reason}). Syntax and search were "
                "refreshed; force a SCIP retry with: "
                f"jarvis index {repo_path} --scip",
                file=sys.stderr,
            )
        elif attempt is not None and attempt.state != "available":
            print(
                f"warning: {slug}: SCIP enrichment {attempt.state} — "
                f"{attempt.reason}. The syntax baseline and search were "
                "still published (exit-0 degraded).",
                file=sys.stderr,
            )
        if attempt is not None and attempt.state == "available" and not has_nav:
            print(
                f"warning: {slug} published with symbols but no navigable positions "
                "(chunks/mentions empty) — per-file navigation will return no results. "
                "Check that the indexer emits occurrence ranges and that scip is >= v0.9.0.",
                file=sys.stderr,
            )
    except Exception as exc:
        text = str(exc)
        reason = next((line for line in text.splitlines() if line.strip()),
                      exc.__class__.__name__)
        if published:
            # The pointer already flipped: the navigation snapshot IS live.
            # Never a degradation, never retirement (spec TSI-04 §5 stage
            # 7) — record the bookkeeping failure and exit nonzero saying
            # exactly what landed. Superseded generations are NOT deleted
            # yet (cleanup runs strictly after the registry write), so the
            # next successful run retires them.
            note = (
                f"{reason} — the navigation snapshot is already live "
                f"({versioned_name}); recording its registry state failed"
            )
            _record_failure_best_effort(registry, slug, repo_path,
                                        resolved_language, note, text)
            raise IndexingError(note) from exc
        _record_failure_best_effort(registry, slug, repo_path, resolved_language,
                                    reason, text)
        raise IndexingError(str(exc)) from exc

    # Cleanup strictly AFTER the registry write (spec TSI-04): a crash
    # before this line leaves superseded generations on disk with the new
    # snapshot live AND recorded — recoverable, auditable, and cleaned by
    # the next successful run. A cleanup failure cannot un-publish or
    # un-record anything, so it warns instead of failing the run.
    assert versioned_name is not None
    try:
        _retire_superseded_snapshots(slug, root, keep=versioned_name)
    except Exception as cleanup_exc:
        print(
            f"warning: retiring superseded snapshots for {slug} failed — "
            f"{cleanup_exc}. The new snapshot is live and recorded; the "
            "next successful index cleans them up.",
            file=sys.stderr,
        )
    finally:
        registry.close()

    return slug


def _cmd_index(args: argparse.Namespace) -> int:
    raw_include = getattr(args, "semantic_include", None)
    try:
        slug = index_repo(
            Path(args.path), slug=args.slug, scheme=getattr(args, "scheme", None),
            semantic_include=tuple(raw_include) if raw_include is not None else None,
            language=getattr(args, "language", None),
            scip=getattr(args, "scip", None),
            semantic=getattr(args, "semantic", None) is not False,
        )
    except (UnsupportedLanguageError, NotAGitRepositoryError, IndexingError,
            jobs.BuildLockHeld, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"indexed {slug}")
    # SEMA-01/SEMA-02: the semantic-extra install offer. Post-publish by
    # design — the index is already live before anything interactive can
    # delay or risk it (a consented install re-runs the stage below and
    # stamps the row in the same invocation). Four gates, cheapest and
    # most structural first: (1) offer_semantic — set only by the index
    # subparser, so reindex/watch/MCP can never reach this; (2) the
    # extra must actually be missing; (3) both streams must be TTYs;
    # (4) this repo must not have declined before.
    if (
        getattr(args, "offer_semantic", False)
        and not runtime.is_frozen()
        and _semantic_extra_missing()
        and _at_interactive_tty()
    ):
        registry = Registry(config.data_dir() / "registry.db")
        try:
            entry = registry.get(slug)
        finally:
            registry.close()
        if entry is None or not entry.semantic_declined:
            try:
                answer = input("Install semantic search support for this repo? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
                # Locked: EOF/Ctrl-C at the prompt is a decline —
                # remembered, no traceback, index already complete.
                # Undecodable bytes belong here too: input() decodes
                # stdin strict, so pasted binary garbage (bracketed-paste
                # of invalid bytes, non-UTF-8 terminal locales) raises
                # before any answer exists — the parse table's "garbage
                # declines" rule, same remembered-decline outcome.
                answer = ""
            if answer in ("y", "yes"):
                if _install_semantic_extra():
                    importlib.invalidate_caches()
                    include = tuple(entry.semantic_include) if entry is not None else ()
                    if _run_semantic_stage(Path(args.path), slug, None, include):
                        registry = Registry(config.data_dir() / "registry.db")
                        try:
                            registry.mark_semantic_indexed(slug)
                        finally:
                            registry.close()
                else:
                    print(
                        "warning: semantic extra install failed — index completed "
                        f"without semantic; tried: uv pip install --python {sys.executable} "
                        '"jarvis-mcp[semantic]"',
                        file=sys.stderr,
                    )
                    # Failure is not a refusal (locked): no decline bit,
                    # so the next TTY index offers again.
            else:
                registry = Registry(config.data_dir() / "registry.db")
                try:
                    registry.set_semantic_declined(slug, True)
                finally:
                    registry.close()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    registry = Registry(config.data_dir() / "registry.db")
    try:
        for repo in registry.list():
            # D-08: the glyph prefixes the status field so the 5-column
            # TSV order stays parseable by scripts; failed and degraded
            # rows gain a 6th field carrying the reason one-liner —
            # degraded joins the ◐ family (search still answers) with the
            # failure cause riding that reason field. `partial` is a
            # success variant and stays in the ✓ family. The historical
            # `search-only` status string (removed by spec §12; only
            # un-migrated legacy rows could still carry it) keeps ◐ so it
            # never prints a misleading ✓.
            if repo.status == "failed":
                marker = "✗"
            elif repo.status in (DEGRADED_STATUS, "search-only"):
                marker = "◐"
            else:
                marker = "✓"
            line = (f"{repo.slug}\t{marker} {repo.status}\t{repo.language}"
                    f"\t{repo.commit_sha or '-'}\t{repo.path}")
            if repo.status in ("failed", DEGRADED_STATUS):
                line += f"\t{repo.status_reason or repo.status}"
            print(line)
    finally:
        registry.close()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        slug = config.repo_slug(args.slug)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    registry = Registry(config.data_dir() / "registry.db")
    try:
        repo = registry.get(slug)
    finally:
        registry.close()
    if repo is None:
        print(f"error: no such repo: {slug}", file=sys.stderr)
        return 1
    print(f"slug: {repo.slug}\npath: {repo.path}\nlanguage: {repo.language}\nstatus: {repo.status}")
    print(f"commit: {repo.commit_sha or '-'}\nlast_indexed: {repo.last_indexed.isoformat()}")
    # SCIP stage state (spec TSI-06): the row's persisted decision, kept
    # separate from the overall status line above.
    print(f"scipEnabled: {'true' if repo.scip_enabled else 'false'}")
    print(f"scipState: {repo.scip_state or 'unknown'}")
    if repo.scip_failure_reason:
        print(f"scipFailure: {repo.scip_failure_reason}")
    _print_syntax_counts(slug)
    semantic = repo.semantic_indexed_at.isoformat() if repo.semantic_indexed_at else "-"
    print(f"semantic: {semantic}")
    recovery = recovery_for(repo)
    if repo.status_origin or repo.status_reason or recovery is not None:
        print(f"origin: {origin_of(repo)}")
        # Legacy failed rows predate status_reason; the status string is
        # all the cause they carry.
        print(f"cause: {repo.status_reason or repo.status}")
        if recovery is not None:
            print(f"recovery: {recovery}")
    if repo.status_stderr:
        # Display shows only the tail (resolution #4); the status_stderr
        # column itself is persisted unbounded (D-02) -- the full text is
        # one registry read away.
        print()
        for line in repo.status_stderr.splitlines()[-20:]:
            print(line)
        print("full log: persisted in the registry (status_stderr column)")
    return 0


def _print_syntax_counts(slug: str) -> None:
    """Best-effort syntax extraction counts from the published snapshot's
    `jarvis_snapshot` singleton (spec TSI-06 `capabilities.syntax`).
    Never raises, never loads a grammar, never spawns a subprocess: any
    failure — no pointer, legacy snapshot, unreadable db — silently skips
    the block, matching the null-plus-reason status convention."""
    try:
        from jarvis import syntax_index

        cache = config.new_connection_cache()
        try:
            conn, _ = config.get_connection(cache, slug)
            counts = syntax_index.read_snapshot_facts(conn).syntax_counts
        finally:
            cache.close_all()
    except Exception:
        return
    print(
        f"syntax: parsed={counts.parsed} partial={counts.partial} "
        f"failed={counts.failed} skipped={counts.skipped} "
        f"unsupported={counts.unsupported}"
    )


def _cmd_reindex(args: argparse.Namespace) -> int:
    try:
        slug = config.repo_slug(args.slug)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    registry = Registry(config.data_dir() / "registry.db")
    try:
        repo = registry.get(slug)
    finally:
        registry.close()
    if repo is None:
        print(f"error: no such repo: {slug}", file=sys.stderr)
        return 1
    return _cmd_index(argparse.Namespace(
        path=repo.path, slug=repo.slug, scheme=repo.scheme_override,
        semantic_include=list(repo.semantic_include),
        language=repo.language_override,
        scip=getattr(args, "scip", None),
        semantic=getattr(args, "semantic", None) is not False,
    ))


def _pin_zoekt_repo_name(repo_path: Path, slug: str) -> None:
    """Pin the Zoekt repository name to `slug` via `git config zoekt.name`.

    `zoekt-git-index` has no `-meta` flag, so this replaces
    `_write_zoekt_meta`. Its name resolution order is: `zoekt.name` git
    config, else the `origin` remote URL url-escaped (e.g.
    `github.com%2Fowner%2Frepo`), else the directory basename. Every real repo
    has a remote, so without this `searchCode`'s `r:<slug>` filter matches
    nothing and the tool returns zero hits with no error — a silent wrong
    answer.

    `-shard_prefix_override` is NOT a substitute: it renames the shard file
    while leaving the indexed repository name untouched.

    Raises rather than warning: publishing an index whose name cannot be
    pinned produces exactly the silent failure this exists to prevent.
    """
    _run(["git", "-C", str(repo_path), "config", "zoekt.name", slug],
         cwd=repo_path, step="git config zoekt.name")


def _unpin_zoekt_repo_name(repo_path: Path) -> None:
    """Remove the `zoekt.name` pin, so `forget` leaves no footprint in the
    user's repo.

    Best-effort by design: `git config --unset` exits 5 when the key is
    absent (a repo indexed before pinning existed) and non-zero when the
    directory is gone (the user deleted the repo). Neither should fail a
    `forget` whose real work — dropping the registry row, index, and shards —
    has nothing to do with this key.
    """
    subprocess.run(
        ["git", "-C", str(repo_path), "config", "--unset", "zoekt.name"],
        capture_output=True, text=True, check=False,
    )


def _remove_zoekt_shards(slug: str, root: Path | None = None) -> list[Path]:
    """Delete the Zoekt shards belonging to `slug`.

    Zoekt names each shard `<repo-name>_v<N>.<NNNNN>.zoekt`, and Task 4 makes
    `<repo-name>` the slug. The `_v` in the glob is deliberate: a bare
    `slug*` would let "api" also match "api-gateway"'s shard.

    Without this, `forget` left the shard in place and `searchCode` kept
    returning hits for a repo jarvis no longer knows about.
    """
    zoekt_dir = config.data_dir(root) / ".zoekt"
    if not zoekt_dir.is_dir():
        return []
    removed: list[Path] = []
    for shard in sorted(zoekt_dir.glob(f"{slug}_v*.zoekt")):
        shard.unlink(missing_ok=True)
        removed.append(shard)
    return removed


def forget_repo(slug: str) -> tuple[bool, str]:
    """The full `jarvis forget` body, shared with the dashboard's
    POST /api/repos/{slug}/forget. Returns (ok, message); callers decide
    how to surface it (CLI prints, dashboard JSONs)."""
    try:
        slug = config.repo_slug(slug)
    except ValueError as exc:
        return False, str(exc)
    try:
        with jobs.build_lock(slug):
            registry = Registry(config.data_dir() / "registry.db")
            try:
                entry = registry.get(slug)
                existed = registry.forget(slug)
            finally:
                registry.close()
            if not existed:
                return False, f"no such repo: {slug}"
            if entry is not None:
                _unpin_zoekt_repo_name(Path(entry.path))
            # Package-edge teardown (spec TSI-08: refresh forget for all new
            # artifacts while preserving Zoekt unpinning and package-edge
            # teardown): every package this repo owned and every edge touching it
            # dies with the registration. Other repos' identity rows survive —
            # an edge pointing into a forgotten repo's packages would otherwise
            # dangle. This closes the evidence-found gap where forget left the
            # graph rows behind and blastRadius kept answering for a forgotten
            # repo.
            graph_store = GraphStore(config.data_dir() / "registry.db")
            try:
                graph_store.forget_repo(slug)
            finally:
                graph_store.close()
            index_dir = config.index_dir(slug)
            if index_dir.exists():
                shutil.rmtree(index_dir)
            _remove_zoekt_shards(slug)
            shutil.rmtree(config.lancedb_dir() / f"{slug}.lance", ignore_errors=True)
            # D-06: forgetting a repo removes everything jarvis stored for it. The
            # scip-swift cache legitimately may not exist (never-Swift repo, or the
            # binary never ran), hence ignore_errors like the lancedb sweep above.
            shutil.rmtree(config.swift_cache_dir(slug), ignore_errors=True)
            # D-06 continued: the launch record and index log are jarvis state
            # too. Best-effort, like the sweeps above -- a cleanup failure must
            # not fail a forget. The lock FILE itself is preserved: see
            # jobs.clear_job_files.
            jobs.clear_job_files(slug)
    except jobs.BuildLockHeld as exc:
        # Destroying a repo's row, graph edges, and artifacts while a writer
        # is mid-run corrupts that run and can resurrect artifacts the forget
        # already removed.
        return False, str(exc)
    return True, f"forgot {slug}"


def _cmd_forget(args: argparse.Namespace) -> int:
    ok, message = forget_repo(args.slug)
    if not ok:
        print(f"error: {message}", file=sys.stderr)
        return 1
    print(message)
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Watch `path` for source-file changes and debounce-reindex it.

    Not a daemon requirement — an optional foreground command a user runs
    while actively editing. `watchdog` is an optional dependency (`uv sync
    --extra watch`); its import is deferred so a base install never needs
    it just to run `jarvis index`/`list`/`status`."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print(
            "error: `watchdog` is required for `jarvis watch` — install with `uv sync --extra watch`",
            file=sys.stderr,
        )
        return 1

    repo_path = Path(args.path).resolve()
    try:
        slug = config.repo_slug(args.slug or repo_path.name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    def _reindex() -> None:
        print(f"[watch] change detected, reindexing {slug} ...")
        try:
            # Spec TSI-07: the SCIP-only suppression lives INSIDE
            # index_repo (via watch=True), replacing FALL-05's whole-run
            # skip. Every debounced event still runs the syntax baseline,
            # the optional semantic stage, and publication — only a
            # known-failing SCIP attempt at an unchanged commit is
            # skipped, with a diagnostic naming the retry command.
            # Debounce semantics are unchanged; watch.py stays pure.
            index_repo(
                repo_path, slug=slug, scheme=args.scheme, language=args.language,
                scip=getattr(args, "scip", None),
                semantic=getattr(args, "semantic", None) is not False, watch=True,
            )
            print(f"[watch] {slug} reindexed")
        except Exception as exc:
            # Broad on purpose: index_repo() can raise before its own
            # try/except is even entered (e.g. `_git_head()`'s subprocess
            # call, or the first Registry() connection) — anything short
            # of catching Exception here would let a single transient
            # failure (a locked registry.db, a momentarily-corrupt .git)
            # kill the whole watch process instead of just skipping this
            # one reindex and continuing to watch.
            print(f"[watch] reindex failed: {exc}", file=sys.stderr)

    debouncer = Debouncer(delay_seconds=args.debounce, on_fire=_reindex)

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            if event.is_directory or should_ignore_path(event.src_path):
                return
            debouncer.notify()

    observer = Observer()
    observer.schedule(_Handler(), str(repo_path), recursive=True)
    observer.start()
    print(f"[watch] watching {repo_path} (slug={slug}, debounce={args.debounce}s) — Ctrl+C to stop")
    try:
        while True:
            time.sleep(0.5)
            try:
                debouncer.poll()
            except Exception as exc:
                # Second line of defense: _reindex() already catches
                # broadly, but the watch loop itself must never die from
                # an unexpected error — that would silently stop watching
                # with no obvious signal beyond a scrollback line.
                print(f"[watch] unexpected error, still watching: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the localhost dashboard (blocks until Ctrl-C)."""
    from jarvis import dashboard

    try:
        dashboard.serve(args.port, open_browser=not args.no_open)
    except OSError as exc:  # e.g. port already in use
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0



_REMOVED_OPTIONS: dict[str, str] = {
    # Superseded by reversible --scip/--no-scip and the always-on syntax
    # baseline (spec TSI-07/TSI-08, §12). Rejected with the replacement —
    # never silently mapped — so a stale script fails loudly instead of
    # quietly changing meaning.
    "--search-only": (
        "--no-scip (the syntax baseline is always indexed; SCIP is optional enrichment)"
    ),
    "--fallback-search-only": "--scip (SCIP failures degrade to exit-0 degraded automatically)",
    "--no-fallback-search-only": "--scip (SCIP retries automatically on the next explicit run)",
}


def _add_scip_flag(parser: argparse.ArgumentParser) -> None:
    """The mutually exclusive `--scip` / `--no-scip` pair (spec TSI-07).
    One tri-state dest: omitted = use the persisted choice, defaulting to
    enabled for a new repo; explicit = update the persisted choice."""
    parser.add_argument(
        "--scip",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="scip",
        help="attempt SCIP enrichment (default: the persisted choice, "
             "enabled for a new repo; a failing SCIP attempt degrades to "
             "exit-0 degraded — the syntax baseline always publishes)",
    )


def _add_semantic_flag(parser: argparse.ArgumentParser) -> None:
    """The mutually exclusive `--semantic` / `--no-semantic` pair. One
    tri-state dest, mirroring `_add_scip_flag`: omitted means "leave the
    default in force" (the stage runs when the extra is installed), explicit
    means the caller decided. Unlike --scip this is NOT persisted -- it is a
    per-run cost decision, not a property of the repo."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--semantic", dest="semantic", action="store_true", default=None,
        help="build the semantic (vector) index when the `semantic` extra is installed",
    )
    group.add_argument(
        "--no-semantic", dest="semantic", action="store_false", default=None,
        help="skip the semantic stage even when the `semantic` extra is installed",
    )


def _reject_removed_options(argv: list[str]) -> None:
    for arg in argv:
        if arg.split("=", 1)[0] in _REMOVED_OPTIONS:
            flag = arg.split("=", 1)[0]
            print(
                f"error: {flag} was removed — use {_REMOVED_OPTIONS[flag]}",
                file=sys.stderr,
            )
            raise SystemExit(2)


def _warn_removed_env() -> None:
    """JARVIS_FALLBACK_SEARCH_ONLY no longer controls anything (spec
    §12): say so once per invocation rather than silently ignoring a
    variable a user's shell profile may still export."""
    if os.environ.get("JARVIS_FALLBACK_SEARCH_ONLY") is not None:
        print(
            "note: JARVIS_FALLBACK_SEARCH_ONLY is no longer read — "
            "use --scip/--no-scip (persisted per repo) instead",
            file=sys.stderr,
        )


def _distribution_version() -> str:
    try:
        return distribution_version("jarvis-mcp")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument(
        "--version", action="version", version=f"jarvis {_distribution_version()}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="index a repo")
    index_parser.add_argument("path", help="path to the repo to index")
    index_parser.add_argument("--slug", help="override the auto-derived slug")
    index_parser.add_argument(
        "--scheme", help="Xcode scheme to build (Swift repos using xcodebuild with more than one scheme)"
    )
    index_parser.add_argument(
        "--semantic-include",
        action="append",
        metavar="PATH",
        help="force-include a path prefix the generated-file filter would skip "
             "(repeatable; persisted and reused by reindex/watch)",
    )
    index_parser.add_argument(
        "--language",
        choices=sorted(_INDEXER_BY_LANGUAGE),
        help="select the SCIP enrichment language instead of detecting it "
             "from git-tracked files (never restricts syntax coverage; "
             "persisted and reused by reindex/watch)",
    )
    _add_scip_flag(index_parser)
    _add_semantic_flag(index_parser)
    index_parser.set_defaults(func=_cmd_index)
    # SEMA-02 structural gate: only `jarvis index` offers — reindex's
    # synthetic Namespace, watch's index_repo call, and MCP paths all
    # read False via getattr's default.
    index_parser.set_defaults(offer_semantic=True)

    list_parser = subparsers.add_parser("list", help="list indexed repos")
    list_parser.set_defaults(func=_cmd_list)

    status_parser = subparsers.add_parser("status", help="show a repo's index status")
    status_parser.add_argument("slug")
    status_parser.set_defaults(func=_cmd_status)

    reindex_parser = subparsers.add_parser("reindex", help="re-run indexing for a registered repo")
    reindex_parser.add_argument("slug")
    _add_scip_flag(reindex_parser)
    _add_semantic_flag(reindex_parser)
    reindex_parser.set_defaults(func=_cmd_reindex)

    install_semantic_parser = subparsers.add_parser(
        "install-semantic",
        help="install source-build semantic dependencies",
    )
    install_semantic_parser.set_defaults(func=_cmd_install_semantic)

    forget_parser = subparsers.add_parser("forget", help="remove a repo's registration and published index")
    forget_parser.add_argument("slug")
    forget_parser.set_defaults(func=_cmd_forget)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="serve the localhost operator dashboard")
    dashboard_parser.add_argument("--port", type=int, default=None,
                                  help="port (default: JARVIS_DASHBOARD_PORT or 6080)")
    dashboard_parser.add_argument("--no-open", action="store_true",
                                  help="do not open the browser automatically")
    dashboard_parser.set_defaults(func=_cmd_dashboard)

    watch_parser = subparsers.add_parser("watch", help="watch a repo and debounce-reindex on change")
    watch_parser.add_argument("path", help="path to the repo to watch")
    watch_parser.add_argument("--slug", help="override the auto-derived slug")
    watch_parser.add_argument(
        "--scheme", help="Xcode scheme to build (Swift repos using xcodebuild with more than one scheme)"
    )
    watch_parser.add_argument("--debounce", type=float, default=5.0, help="quiet-period seconds (default: 5.0)")
    watch_parser.add_argument(
        "--language",
        choices=sorted(_INDEXER_BY_LANGUAGE),
        help="select the SCIP enrichment language instead of detecting it "
             "from git-tracked files (never restricts syntax coverage; "
             "persisted and reused by reindex/watch)",
    )
    _add_scip_flag(watch_parser)
    _add_semantic_flag(watch_parser)
    watch_parser.set_defaults(func=_cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> None:
    effective = sys.argv[1:] if argv is None else list(argv)
    _reject_removed_options(effective)
    _warn_removed_env()
    parser = build_parser()
    args = parser.parse_args(effective)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
