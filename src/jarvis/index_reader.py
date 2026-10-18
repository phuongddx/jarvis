"""Filestore path resolution + a read-only SQLite connection cache.

Layout (matches Phase 2's ``pipeline/publish.sh`` GCS_PREFIX convention,
mirrored under the Filestore mount once/if the Phase-1 spike lands it):

    {filestore_root}/scip/{project}/{repo}/{branch}/
        current                        <- pointer file, one line: "index-<sha>.db"
        index-<sha>.db                 <- versioned, NEVER mutated in place
        index-<sha>.metadata.json      <- sibling metadata (commit/generatedAt/etc.)
        index-<other-sha>.db           <- older versions kept around

Two red-team fixes drive the design here:

* **C4 (NFS-safe reads):** versioned filenames are never mutated in place —
  only the small ``current`` pointer file's CONTENTS change. Connections are
  opened ``mode=ro`` against the versioned file; ``immutable=1`` is safe here
  specifically because that filename is never swapped in place once written.
* **Cache invalidation by the published pointer changing, NOT by mtime**
  (phase-03's Architecture section) — mtime on a networked filesystem is not
  a reliable signal (NFS caching, clock skew); the pointer file's CONTENT is
  the authoritative version identifier, so the cache key is
  ``(project, repo, branch, pointer_content)``. A cache entry for a stale
  pointer value is simply never looked up again once the pointer changes —
  it can be evicted lazily by the cache's max-size bound.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BRANCH_MARKER = "default"
POINTER_FILENAME = "current"


class IndexNotFoundError(Exception):
    """Raised when no published index exists for the requested coordinates."""


@dataclass(frozen=True)
class IndexMetadata:
    """Parsed contents of a version's sibling ``*.metadata.json`` file."""

    project: str
    repo: str
    branch: str
    commit_sha: str | None
    published_at: str | None  # ISO-8601 UTC string as written by publish.sh
    # Additive (Task 3, spec TSI-04 section 5): the syntax snapshot's own
    # generation id and source-manifest digest, when this metadata sibles
    # a snapshot that carries `jarvis_snapshot`. Old metadata.json files
    # (and any legacy publish.sh writer) never populate these keys, so
    # they default to None -- old metadata stays fully readable.
    generation: str | None = None
    source_hash: str | None = None
    # Carry-forward provenance (spec: incremental-reindex). Old writers
    # never emit these keys; old metadata stays fully readable.
    scip_mode: str | None = None
    carried_from_sha: str | None = None
    carry_depth: int = 0

    @classmethod
    def from_json(cls, payload: dict) -> "IndexMetadata":
        return cls(
            project=payload["project"],
            repo=payload["repo"],
            branch=payload["branch"],
            commit_sha=payload.get("commit_sha"),
            published_at=payload.get("published_at"),
            generation=payload.get("generation"),
            source_hash=payload.get("source_hash"),
            scip_mode=payload.get("scip_mode"),
            carried_from_sha=payload.get("carried_from_sha"),
            carry_depth=int(payload.get("carry_depth") or 0),
        )


def _index_dir(filestore_root: str, project: str, repo: str, branch: str) -> Path:
    return Path(filestore_root) / "scip" / project / repo / branch


def read_pointer(filestore_root: str, project: str, repo: str, branch: str) -> str:
    """Read the ``current`` pointer file's content (a versioned db filename).

    Raises IndexNotFoundError if no index has ever been published for these
    coordinates — the caller must map this to getIndexStatus's
    ``indexed=false`` / a 404, never to an unrelated 500.
    """
    pointer_path = _index_dir(filestore_root, project, repo, branch) / POINTER_FILENAME
    try:
        content = pointer_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise IndexNotFoundError(f"no published index for {project}/{repo}@{branch}") from exc
    if not content:
        raise IndexNotFoundError(f"empty pointer file for {project}/{repo}@{branch}")
    return content


def read_metadata(filestore_root: str, project: str, repo: str, branch: str, pointer_content: str) -> IndexMetadata | None:
    """Read the versioned metadata.json sibling of the current pointer's db file.

    Returns None (never raises) on any failure to read/parse — the freshness
    contract (red-team fix H11) requires callers to degrade to
    freshness="unknown" rather than fail the whole query when metadata is
    missing or malformed.
    """
    metadata_filename = pointer_content.removesuffix(".db") + ".metadata.json"
    metadata_path = _index_dir(filestore_root, project, repo, branch) / metadata_filename
    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    # Preserve the existing legacy-reader fallbacks for omitted coordinates;
    # from_json itself represents the canonical metadata contract.
    payload = dict(raw)
    payload.setdefault("project", project)
    payload.setdefault("repo", repo)
    payload.setdefault("branch", branch)
    return IndexMetadata.from_json(payload)


class IndexConnectionCache:
    """Per-(project, repo, branch, pointer) cache of read-only SQLite connections.

    Not a generic LRU library dependency — the eviction policy is a simple
    bounded FIFO/LRU hybrid via OrderedDict, sufficient for a cache whose
    entries are cheap to reopen (mode=ro against a never-mutated file).
    Thread-safe: FastAPI/uvicorn may serve requests across a threadpool for
    sync SQLite calls, so a lock guards the shared dict.
    """

    def __init__(self, filestore_root: str, max_size: int = 64) -> None:
        self._filestore_root = filestore_root
        self._max_size = max_size
        self._lock = threading.Lock()
        self._connections: OrderedDict[tuple[str, str, str, str], sqlite3.Connection] = OrderedDict()

    def get_connection(self, project: str, repo: str, branch: str) -> tuple[sqlite3.Connection, IndexMetadata | None]:
        """Resolve the current pointer, then return a cached (or freshly
        opened) read-only connection plus that version's parsed metadata.

        Raises IndexNotFoundError if no index has been published yet.
        """
        pointer_content = read_pointer(self._filestore_root, project, repo, branch)
        key = (project, repo, branch, pointer_content)

        with self._lock:
            conn = self._connections.get(key)
            if conn is not None:
                self._connections.move_to_end(key)
                metadata = read_metadata(self._filestore_root, project, repo, branch, pointer_content)
                return conn, metadata

        db_path = _index_dir(self._filestore_root, project, repo, branch) / pointer_content
        if not db_path.exists():
            raise IndexNotFoundError(
                f"pointer for {project}/{repo}@{branch} references missing file {pointer_content}"
            )

        # mode=ro: never write to a versioned artifact. immutable=1 is safe
        # ONLY because this exact filename is never swapped in place once
        # written (red-team fix C4) — a new commit gets a new filename, and
        # only the small pointer file's contents change.
        uri = f"file:{db_path}?mode=ro&immutable=1"
        new_conn = sqlite3.connect(uri, uri=True, check_same_thread=False)

        with self._lock:
            self._connections[key] = new_conn
            self._connections.move_to_end(key)
            while len(self._connections) > self._max_size:
                _, evicted = self._connections.popitem(last=False)
                evicted.close()

        metadata = read_metadata(self._filestore_root, project, repo, branch, pointer_content)
        return new_conn, metadata

    def close_all(self) -> None:
        with self._lock:
            for conn in self._connections.values():
                conn.close()
            self._connections.clear()
