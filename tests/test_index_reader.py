"""Tests for index_reader.py's Filestore path resolution + connection cache
pointer-based invalidation (NOT mtime-based, per phase-03's Architecture
section and red-team fix C4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jarvis.index_reader import (
    IndexConnectionCache,
    IndexNotFoundError,
    read_metadata,
    read_pointer,
)
from tests.fixtures.synthetic_index import (
    COMMIT_SHA,
    build_published_index,
    build_synthetic_index_db,
)


def test_metadata_defaults_when_carry_keys_absent():
    from jarvis.index_reader import IndexMetadata

    meta = IndexMetadata.from_json(
        {
            "project": "_",
            "repo": "demo",
            "branch": "_",
            "commit_sha": "abc",
            "published_at": "2026-10-18T00:00:00Z",
        }
    )
    assert (meta.scip_mode, meta.carried_from_sha, meta.carry_depth) == (None, None, 0)


def test_metadata_parses_carry_fields():
    from jarvis.index_reader import IndexMetadata

    meta = IndexMetadata.from_json(
        {
            "project": "_",
            "repo": "demo",
            "branch": "_",
            "commit_sha": "abc",
            "scip_mode": "carried",
            "carried_from_sha": "prev000",
            "carry_depth": 3,
        }
    )
    assert meta.scip_mode == "carried"
    assert meta.carried_from_sha == "prev000"
    assert meta.carry_depth == 3


def test_read_pointer_missing_raises_index_not_found(tmp_path: Path):
    with pytest.raises(IndexNotFoundError):
        read_pointer(str(tmp_path), "nope", "nope", "main")


def test_read_pointer_returns_versioned_filename(tmp_path: Path):
    build_published_index(tmp_path, "acme", "toy-repo", "main")
    pointer = read_pointer(str(tmp_path), "acme", "toy-repo", "main")
    assert pointer == f"index-{COMMIT_SHA}.db"


def test_read_metadata_missing_file_returns_none_not_raise(tmp_path: Path):
    build_published_index(tmp_path, "acme", "toy-repo", "main")
    result = read_metadata(str(tmp_path), "acme", "toy-repo", "main", "index-does-not-exist.db")
    assert result is None


def test_read_metadata_malformed_json_returns_none(tmp_path: Path):
    index_dir = tmp_path / "scip" / "acme" / "toy-repo" / "main"
    index_dir.mkdir(parents=True)
    (index_dir / "index-bad.metadata.json").write_text("{not json", encoding="utf-8")
    result = read_metadata(str(tmp_path), "acme", "toy-repo", "main", "index-bad.db")
    assert result is None


def test_connection_cache_opens_readonly_connection(tmp_path: Path):
    build_published_index(tmp_path, "acme", "toy-repo", "main")
    cache = IndexConnectionCache(str(tmp_path))
    conn, metadata = cache.get_connection("acme", "toy-repo", "main")
    assert metadata is not None
    assert metadata.commit_sha == COMMIT_SHA
    row = conn.execute("SELECT COUNT(*) FROM global_symbols").fetchone()
    assert row[0] == 5
    # Read-only: a write attempt against an immutable=1 connection must fail.
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO documents (relative_path) VALUES ('x')")
    cache.close_all()


def test_connection_cache_reuses_connection_for_same_pointer(tmp_path: Path):
    build_published_index(tmp_path, "acme", "toy-repo", "main")
    cache = IndexConnectionCache(str(tmp_path))
    conn1, _ = cache.get_connection("acme", "toy-repo", "main")
    conn2, _ = cache.get_connection("acme", "toy-repo", "main")
    assert conn1 is conn2
    cache.close_all()


def test_connection_cache_invalidates_on_pointer_change_not_mtime(tmp_path: Path):
    """The cache key is the pointer's CONTENT, not the file's mtime — verify
    that publishing a new version (new pointer content) yields a distinct
    connection even though we don't touch/backdate any mtime."""
    index_dir = build_published_index(tmp_path, "acme", "toy-repo", "main")
    cache = IndexConnectionCache(str(tmp_path))
    conn1, metadata1 = cache.get_connection("acme", "toy-repo", "main")
    assert metadata1.commit_sha == COMMIT_SHA

    # Publish a new version: new versioned db file, new pointer content.
    new_db_name = "index-def5678.db"
    build_synthetic_index_db(index_dir / new_db_name)
    (index_dir / "index-def5678.metadata.json").write_text(
        '{"project": "acme", "repo": "toy-repo", "branch": "main", '
        '"commit_sha": "def5678", "published_at": "2026-07-09T00:00:00Z"}',
        encoding="utf-8",
    )
    (index_dir / "current").write_text(new_db_name, encoding="utf-8")

    conn2, metadata2 = cache.get_connection("acme", "toy-repo", "main")
    assert conn2 is not conn1
    assert metadata2.commit_sha == "def5678"
    cache.close_all()


def test_connection_cache_evicts_oldest_beyond_max_size(tmp_path: Path):
    for i in range(3):
        build_published_index(tmp_path, "acme", f"repo-{i}", "main")
    cache = IndexConnectionCache(str(tmp_path), max_size=2)
    cache.get_connection("acme", "repo-0", "main")
    cache.get_connection("acme", "repo-1", "main")
    cache.get_connection("acme", "repo-2", "main")
    assert len(cache._connections) == 2
    assert ("acme", "repo-0", "main", f"index-{COMMIT_SHA}.db") not in cache._connections
    cache.close_all()
