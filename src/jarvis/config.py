"""Data-dir and repo-slug resolution for jarvis's single-tenant layout.

`index_reader.IndexConnectionCache` keys entries on the vendored source's 3-tuple
(project, repo, branch) — vendored unchanged (Phase 1). jarvis has no
project/branch dimension (single user, one index per repo), so those two
are pinned constants here and every repo maps to a bare slug. On disk this
resolves to ``{data_dir}/scip/_/{slug}/_/`` — the two ``_`` segments are an
artifact of reusing the vendored cache's path shape unchanged rather than a
user-facing contract.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from jarvis.index_reader import IndexConnectionCache

DEFAULT_DATA_DIR = Path.home() / ".jarvis"

PROJECT = "_"
BRANCH = "_"

IGNORED_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build", "DerivedData", ".build"}


def lancedb_dir(root: Path | None = None) -> Path:
    """Directory holding one LanceDB table per repo (semantic search vectors)."""
    return data_dir(root) / "lancedb"


def swift_cache_dir(slug: str, root: Path | None = None) -> Path:
    """Per-repo scip-swift incremental-cache directory (D-05): keyed by
    slug so IndexStores from different repos never interfere.

    jarvis computes this path only — upstream scip-swift creates the
    directory, and its manifest wholesale-invalidates the cache when the
    binary or toolchain changes, so no jarvis-side versioning is needed."""
    return data_dir(root) / "cache" / "scip-swift" / slug


def index_lockfile(slug: str, root: Path | None = None) -> Path:
    """Per-slug build lock. Held (via `flock`) for the whole duration of one
    index run by whichever process is writing — CLI, watch, or an
    MCP-spawned child alike. Never unlinked while jarvis is running: removing
    a file another waiter still holds open is the classic flock footgun."""
    return data_dir(root) / f"index-{slug}.lock"


def index_launchfile(slug: str, root: Path | None = None) -> Path:
    """Single-writer record of an index run that has been spawned but has not
    yet acquired the build lock. Written once, before spawn, by the process
    doing the spawning; never mutated, and never touched by the child."""
    return data_dir(root) / f"index-{slug}.launch"


def index_log(slug: str, root: Path | None = None) -> Path:
    """stderr of an MCP-spawned index child. The child must never inherit the
    server's stdio — that stream is the MCP transport."""
    return data_dir(root) / f"index-{slug}.log"


def shim_dir(root: Path | None = None) -> Path:
    """Directory holding shims for system tools whose default version is too
    old for an indexer to use.

    Currently just `bash`: scip-java's generated javac wrapper is
    `#!/usr/bin/env bash` with `set -eu` and an unguarded `"${LAUNCHER_ARGS[@]}"`,
    which errors on bash < 4.4 — the bash macOS ships. `setup.sh --only
    bash-shim` writes the symlink here; `index_cli._java_indexer_env()` puts
    this directory first on PATH for the indexer subprocess.

    Deliberately NOT under `bin/`: that holds optional indexer binaries
    setup.sh downloaded and owns, this holds links to system tools it did not.
    """
    return data_dir(root) / "shims"


_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def data_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override
    raw = os.environ.get("JARVIS_DATA_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR



def repo_slug(name: str) -> str:
    """Normalize a user-chosen repo name into a directory-safe slug.

    Rejects `.`/`..` explicitly (not just `/`) — both survive the
    character-class substitution below unchanged since `.` is an allowed
    slug character, but either one alone is a path-traversal component
    once joined under `index_dir()`."""
    slug = _SLUG_UNSAFE.sub("-", name.strip().lower()).strip("-")
    if not slug or slug in (".", ".."):
        raise ValueError(f"repo name {name!r} does not produce a usable, safe slug")
    return slug


def index_dir(slug: str, root: Path | None = None) -> Path:
    """The directory holding a repo's ``current`` pointer + versioned db files."""
    return (root or data_dir()) / "scip" / PROJECT / slug / BRANCH


def new_connection_cache(root: Path | None = None) -> IndexConnectionCache:
    return IndexConnectionCache(str(root or data_dir()))


def get_connection(cache: IndexConnectionCache, slug: str):
    return cache.get_connection(PROJECT, slug, BRANCH)


DASHBOARD_DEFAULT_PORT = 6080


def dashboard_port() -> int:
    """`JARVIS_DASHBOARD_PORT` overrides the localhost dashboard port; an
    invalid value (unparseable, or outside the 1-65535 TCP port range)
    degrades to the default rather than breaking `jarvis dashboard` (same
    contract as zoekt's port override)."""
    raw = os.environ.get("JARVIS_DASHBOARD_PORT", "")
    try:
        port = int(raw) if raw else DASHBOARD_DEFAULT_PORT
        if not 1 <= port <= 65535:
            raise ValueError(raw)
    except ValueError:
        print(
            f"warning: invalid JARVIS_DASHBOARD_PORT {raw!r}; "
            f"using {DASHBOARD_DEFAULT_PORT}",
            file=sys.stderr,
        )
        return DASHBOARD_DEFAULT_PORT
    return port
