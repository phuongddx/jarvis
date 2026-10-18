# Incremental Reindex Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use text2prod:subagent-driven-development (recommended) or text2prod:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `jarvis index` no-op on identical runs and, on watch, skip the SCIP indexer by carrying forward unaffected rows from the previous snapshot.

**Architecture:** B guards after stage-2 capture (HEAD sha + source_hash vs published metadata) → `"unchanged"`, zero registry writes. A is watch-only: `carry.py` gates eligibility (carryable state, depth < 5, closure ≤ 60%), builds the new SCIP DB as a filtered copy (FK-safe deletes, orphan retention, `carried_documents` sidecar, recomputed counters), returned as `ScipAttempt("carried")`. Provenance flows through `.metadata.json`, registry columns, `FreshnessSnapshot`, per-`Location` labels, `getIndexStatus`.

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `dataclasses`, `shutil`), pytest, existing jarvis modules only.

**Spec:** [spec.md](spec.md)

## Global Constraints

- Python ≥3.12,<3.15; `X | None`, builtin generics, frozen dataclasses; no `async def`.
- Raw stdlib `sqlite3`, parameterized SQL; registry migrations idempotent (`_ensure_column`).
- Atomic publish preserved: immutable `index-<sha>-<generation>.db` + `.metadata.json` sibling, `current` flip via `os.replace`; readers `mode=ro&immutable=1`.
- Skipped/degraded SCIP never fails the run; every carry failure degrades to the full pipeline.
- Depth cap 5; closure (changed+closure)/prev_docs > 0.60 → full.
- Explicit `jarvis index`/server `indexRepo` = full + no-op guard only; `watch=True` = carry. No new CLI flags.
- No `global_symbols` pruning; no symbol-ID remapping.
- Old `.metadata.json` stays readable (new keys default `None`/`0`).
- Unit tests mock subprocess/network; state tests set `JARVIS_DATA_DIR=tmp_path`.
- Commits: lowercase imperative Conventional Commits; unit gate green before each commit.

---

### Task 1: Registry — `carried` state + carry columns

**Files:** Modify `src/jarvis/registry.py` (`SCIP_STATES` ~L42; migrations ~L372; `ScipStageFields` ~L209; `RegisteredRepo` ~L226; `_SELECT_COLUMNS` ~L91; `upsert` ~L380; `_row_to_repo` ~L268); test `tests/test_registry.py`.

**Interfaces:** Produces `SCIP_STATES ∋ "carried"`; `ScipStageFields(state, failure_reason=None, failure_stderr=None, failed_at_sha=None, carried_from_sha: str|None = None, carry_depth: int = 0)`; `RegisteredRepo.scip_carried_from_sha: str|None = None`, `.scip_carry_depth: int = 0`. Consumed by Tasks 5-7.
- [ ] **Step 1: Failing tests** — append to `tests/test_registry.py`:
```python
def test_scip_states_include_carried():
    from jarvis.registry import SCIP_STATES
    assert "carried" in SCIP_STATES

def test_upsert_carries_scip_carry_fields(tmp_path):
    from jarvis.registry import Registry, ScipStageFields
    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("demo", "/tmp/demo", "python", "abc", "indexed",
                        scip_stage=ScipStageFields(
                            state="carried", carried_from_sha="prev000", carry_depth=2))
        row = registry.get("demo")
        assert row.scip_state == "carried"
        assert row.scip_carried_from_sha == "prev000"
        assert row.scip_carry_depth == 2
    finally:
        registry.close()

def test_upsert_rejects_composite_carried_state(tmp_path):
    import pytest
    from jarvis.registry import Registry, ScipStageFields
    registry = Registry(tmp_path / "registry.db")
    try:
        with pytest.raises(ValueError, match="invalid scip_state"):
            registry.upsert("demo", "/tmp/demo", "python", "abc", "indexed",
                            scip_stage=ScipStageFields(state="carried@abc"))
    finally:
        registry.close()

def test_carry_columns_migrate_idempotently(tmp_path):
    import sqlite3
    from jarvis.registry import Registry
    db = tmp_path / "registry.db"
    Registry(db).close(); Registry(db).close()
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(repos)")}
    finally:
        conn.close()
    assert {"scip_carried_from_sha", "scip_carry_depth"} <= cols
```
- [ ] **Step 2: Verify failure** — run `uv run pytest tests/test_registry.py -k "carried or carry_columns" -v` → expect FAIL (`"carried" not in SCIP_STATES`).
- [ ] **Step 3: Implement**
```python
SCIP_STATES = frozenset(
    {"available", "partial", "failed", "unavailable", "unsupported", "disabled", "carried"}
)
# migrations, after scip_failed_at_sha:
_ensure_column(self._conn, "scip_carried_from_sha", "TEXT")
_ensure_column(self._conn, "scip_carry_depth", "INTEGER NOT NULL DEFAULT 0")
# ScipStageFields + RegisteredRepo: append the two fields shown in Interfaces.
# _SELECT_COLUMNS: append ", scip_carried_from_sha, scip_carry_depth".
# upsert() scip_stage branch: add both columns to INSERT list + two "?",
# conflict list += "scip_carried_from_sha=excluded.scip_carried_from_sha,
# scip_carry_depth=excluded.scip_carry_depth", values tuple +=
# (scip_stage.carried_from_sha, scip_stage.carry_depth); pass both to the
# returned RegisteredRepo.
# _row_to_repo(): unpack the two names between scip_failed_at_sha and
# semantic_declined; pass through.
```
- [ ] **Step 4: Verify pass** — run `uv run pytest tests/test_registry.py -v` → PASS.
- [ ] **Step 5: Commit**

`git add src/jarvis/registry.py tests/test_registry.py` → commit `feat(registry): add carried scip state and carry columns`

### Task 2: `IndexMetadata` carry fields

**Files:** Modify `src/jarvis/index_reader.py` (`IndexMetadata` ~L45 + JSON read path); test `tests/test_index_reader.py` (create if absent).

**Interfaces:** Produces `IndexMetadata.scip_mode: str|None = None`, `.carried_from_sha: str|None = None`, `.carry_depth: int = 0`, plus `classmethod IndexMetadata.from_json(payload: dict) -> IndexMetadata` applying those defaults. Consumed by Tasks 3-5, 7.
- [ ] **Step 1: Failing tests** — append to `tests/test_index_reader.py`:
```python
def test_metadata_defaults_when_carry_keys_absent():
    from jarvis.index_reader import IndexMetadata
    meta = IndexMetadata.from_json({
        "project": "_", "repo": "demo", "branch": "_",
        "commit_sha": "abc", "published_at": "2026-10-18T00:00:00Z",
    })
    assert (meta.scip_mode, meta.carried_from_sha, meta.carry_depth) == (None, None, 0)

def test_metadata_parses_carry_fields():
    from jarvis.index_reader import IndexMetadata
    meta = IndexMetadata.from_json({
        "project": "_", "repo": "demo", "branch": "_",
        "commit_sha": "abc", "scip_mode": "carried",
        "carried_from_sha": "prev000", "carry_depth": 3,
    })
    assert meta.scip_mode == "carried"
    assert meta.carried_from_sha == "prev000"
    assert meta.carry_depth == 3
```
- [ ] **Step 2: Verify failure** — run `uv run pytest tests/test_index_reader.py -v` → FAIL (no `scip_mode`/`from_json`).
- [ ] **Step 3: Implement**
```python
# IndexMetadata — append after source_hash:
    # Carry-forward provenance (spec: incremental-reindex). Old writers
    # never emit these keys; old metadata stays fully readable.
    scip_mode: str | None = None
    carried_from_sha: str | None = None
    carry_depth: int = 0

    @classmethod
    def from_json(cls, payload: dict) -> "IndexMetadata":
        return cls(
            project=payload["project"], repo=payload["repo"],
            branch=payload["branch"],
            commit_sha=payload.get("commit_sha"),
            published_at=payload.get("published_at"),
            generation=payload.get("generation"),
            source_hash=payload.get("source_hash"),
            scip_mode=payload.get("scip_mode"),
            carried_from_sha=payload.get("carried_from_sha"),
            carry_depth=int(payload.get("carry_depth") or 0),
        )
```
Route the existing `.metadata.json` read through `IndexMetadata.from_json` (keep behavior for legacy keys identical).
- [ ] **Step 4: Verify pass** — run `uv run pytest tests/test_index_reader.py tests/test_query.py -v` → PASS.
- [ ] **Step 5: Commit**

`git add src/jarvis/index_reader.py tests/test_index_reader.py` → commit `feat(reader): parse scip carry provenance from metadata`

### Task 3: `carry.py` — decision, closure, filtered copy

**Files:** Create `src/jarvis/carry.py`, `tests/fixtures/carry_db.py`, `tests/test_carry.py`.

**Interfaces:** Consumes `IndexMetadata` (Task 2).
- Produces (Tasks 4-5): · `CARRY_DEPTH_CAP = 5`; `CARRY_CLOSURE_THRESHOLD = 0.60`; `CARRYABLE_STATES = frozenset({"available", "carried"})` · `@dataclass(frozen=True) class CarryDecision: carry: bool; reason: str|None; info: IndexMetadata|None` — reason ∈ `{"no-previous","not-carryable","depth-cap","threshold",None}` · `published_metadata(slug_dir: Path) -> IndexMetadata|None` · `changed_paths(prev: sqlite3.Connection, source_hashes: dict[str,str]) -> set[str]` · `closure_paths(prev: sqlite3.Connection, changed: set[str]) -> set[str]` · `decide(meta: IndexMetadata|None, prev_doc_count: int, changed_count: int, closure_count: int) -> CarryDecision` · `build_carried_snapshot(prev_db: Path, dest: Path, drop: set[str], prev_sha: str) -> None` · `pointer_target(slug_dir: Path) -> str` — reads `current`; `""` when missing.
- [ ] **Step 1: Fixture builder** — `tests/fixtures/carry_db.py`:
```python
"""Minimal previous-snapshot fixture: b.py references symbol defined in a.py."""
import sqlite3

def build_prev_snapshot(dest) -> None:
    conn = sqlite3.connect(dest)
    try:
        conn.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, language TEXT,
          relative_path TEXT NOT NULL UNIQUE, position_encoding TEXT, text TEXT);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
          chunk_index INTEGER NOT NULL, start_line INTEGER NOT NULL,
          end_line INTEGER NOT NULL, occurrences BLOB NOT NULL);
        CREATE TABLE global_symbols (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE,
          display_name TEXT, kind INTEGER, documentation TEXT, signature BLOB,
          enclosing_symbol TEXT, relationships BLOB);
        CREATE TABLE mentions (chunk_id INTEGER NOT NULL, symbol_id INTEGER NOT NULL,
          role INTEGER NOT NULL, PRIMARY KEY (chunk_id, symbol_id, role));
        CREATE TABLE defn_enclosing_ranges (id INTEGER PRIMARY KEY, document_id INTEGER,
          symbol_id INTEGER, start_line INTEGER, start_char INTEGER,
          end_line INTEGER, end_char INTEGER);
        CREATE TABLE jarvis_snapshot (singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          format_version INTEGER, generation TEXT, commit_sha TEXT, published_at TEXT,
          source_hash TEXT, scip_state TEXT, scip_outlines INTEGER, scip_definitions INTEGER,
          scip_references INTEGER, scip_calls INTEGER, scip_types INTEGER, syntax_counts TEXT);
        CREATE TABLE syntax_files (path TEXT PRIMARY KEY, file_hash TEXT NOT NULL);
        """)
        conn.executemany("INSERT INTO documents (id, relative_path) VALUES (?, ?)",
                         [(1, "a.py"), (2, "b.py")])
        conn.executemany("INSERT INTO chunks (id, document_id, occurrences) VALUES (?, ?, ?)",
                         [(10, 1, b""), (20, 2, b"")])
        conn.executemany("INSERT INTO global_symbols (id, symbol) VALUES (?, ?)",
                         [(100, "demo a.A."), (101, "demo b.B.")])
        conn.executemany("INSERT INTO mentions VALUES (?, ?, ?)",
                         [(10, 100, 1),   # a.py defines A
                          (20, 100, 0),   # b.py references A
                          (20, 101, 1)])  # b.py defines B
        conn.execute("INSERT INTO defn_enclosing_ranges "
                     "(document_id, symbol_id, start_line, start_char, end_line, end_char) "
                     "VALUES (1, 100, 1, 0, 2, 0)")
        conn.execute("INSERT INTO jarvis_snapshot VALUES (1, 1, 'g0', 'prev000', "
                     "'2026-10-18T00:00:00Z', 'hash0', 'available', 2, 2, 1, 0, 0, '{}')")
        conn.execute("INSERT INTO syntax_files VALUES ('a.py', 'ha1')")
        conn.execute("INSERT INTO syntax_files VALUES ('b.py', 'hb1')")
        conn.commit()
    finally:
        conn.close()
```
- [ ] **Step 2: Failing tests** — `tests/test_carry.py`:
```python
import json
import sqlite3

from tests.fixtures.carry_db import build_prev_snapshot

def _meta(**over):
    from jarvis.index_reader import IndexMetadata
    base = dict(project="_", repo="demo", branch="_", commit_sha="new111",
                scip_mode="available", carry_depth=0)
    base.update(over)
    return IndexMetadata(**base)

def test_decide_gates():
    from jarvis.carry import CARRY_DEPTH_CAP, decide
    assert decide(None, 10, 1, 1).reason == "no-previous"
    assert decide(_meta(scip_mode="failed"), 10, 1, 1).reason == "not-carryable"
    assert decide(_meta(carry_depth=CARRY_DEPTH_CAP), 10, 1, 1).reason == "depth-cap"
    assert decide(_meta(), 2, 1, 1).reason == "threshold"  # (1+1)/2 > 0.60
    ok = decide(_meta(), 30, 2, 3)
    assert ok.carry and ok.reason is None and ok.info.scip_mode == "available"

def test_changed_paths_by_hash(tmp_path):
    from jarvis.carry import changed_paths
    build_prev_snapshot(tmp_path / "prev.db")
    conn = sqlite3.connect(tmp_path / "prev.db")
    try:
        assert changed_paths(conn, {"a.py": "ha1", "b.py": "CHANGED"}) == {"b.py"}
        assert changed_paths(conn, {"a.py": "ha1", "new.py": "x"}) == {"new.py"}
    finally:
        conn.close()

def test_closure_invalidates_referencing_docs(tmp_path):
    from jarvis.carry import closure_paths
    build_prev_snapshot(tmp_path / "prev.db")
    conn = sqlite3.connect(tmp_path / "prev.db")
    try:
        assert closure_paths(conn, {"a.py"}) == {"b.py"}  # b.py references A
        assert closure_paths(conn, {"b.py"}) == set()     # nothing references B
    finally:
        conn.close()

def test_build_drops_given_docs_and_labels(tmp_path):
    from jarvis.carry import build_carried_snapshot
    build_prev_snapshot(tmp_path / "prev.db")
    build_carried_snapshot(tmp_path / "prev.db", tmp_path / "c.db",
                           drop={"a.py", "b.py"}, prev_sha="prev000")
    conn = sqlite3.connect(tmp_path / "c.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0] == 2
        assert conn.execute(
            "SELECT scip_state FROM jarvis_snapshot").fetchone()[0] == "carried"
    finally:
        conn.close()

def test_build_keeps_unaffected_doc_with_sidecar(tmp_path):
    from jarvis.carry import build_carried_snapshot
    build_prev_snapshot(tmp_path / "prev.db")
    build_carried_snapshot(tmp_path / "prev.db", tmp_path / "c.db",
                           drop={"a.py"}, prev_sha="prev000")
    conn = sqlite3.connect(tmp_path / "c.db")
    try:
        assert {r[0] for r in conn.execute(
            "SELECT relative_path FROM documents")} == {"b.py"}
        assert conn.execute(
            "SELECT d.relative_path, c.carried_from_sha FROM carried_documents c "
            "JOIN documents d ON d.id = c.document_id").fetchall() == [("b.py", "prev000")]
    finally:
        conn.close()

def test_published_metadata_reads_pointer_target(tmp_path):
    from jarvis.carry import published_metadata, pointer_target
    slug_dir = tmp_path / "slug"; slug_dir.mkdir()
    (slug_dir / "index-new111-1.db").touch()
    (slug_dir / "index-new111-1.metadata.json").write_text(json.dumps({
        "project": "_", "repo": "demo", "branch": "_",
        "commit_sha": "new111", "scip_mode": "carried",
        "carried_from_sha": "prev000", "carry_depth": 1}))
    (slug_dir / "current").write_text("index-new111-1.db")
    assert pointer_target(slug_dir) == "index-new111-1.db"
    meta = published_metadata(slug_dir)
    assert meta.scip_mode == "carried"
    assert published_metadata(tmp_path / "missing") is None
```
- [ ] **Step 3: Verify failure** — run `uv run pytest tests/test_carry.py -v` → FAIL (`jarvis.carry` missing).
- [ ] **Step 4: Implement `src/jarvis/carry.py`**
```python
"""SCIP carry lane (spec: docs/features/incremental-reindex). decide()
gates eligibility; build_carried_snapshot() produces the filtered copy.
Never publishes — caller owns scratch and pointer."""
from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jarvis.index_reader import IndexMetadata

CARRY_DEPTH_CAP = 5
CARRY_CLOSURE_THRESHOLD = 0.60
CARRYABLE_STATES = frozenset({"available", "carried"})

@dataclass(frozen=True)
class CarryDecision:
    carry: bool
    reason: str | None
    info: IndexMetadata | None

def pointer_target(slug_dir: Path) -> str:
    try:
        return (slug_dir / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def published_metadata(slug_dir: Path) -> IndexMetadata | None:
    """Metadata of `current`'s target, or None. Pointer + sibling json
    only — never a glob — so scratch leftovers cannot masquerade."""
    pointer = pointer_target(slug_dir)
    if not pointer:
        return None
    meta_path = slug_dir / (pointer.removesuffix(".db") + ".metadata.json")
    try:
        return IndexMetadata.from_json(
            json.loads(meta_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None

def changed_paths(prev: sqlite3.Connection, source_hashes: dict[str, str]) -> set[str]:
    """Stored-hash diffs from this capture + new captured paths."""
    rows = prev.execute("SELECT path, file_hash FROM syntax_files").fetchall()
    stored = {path: digest for path, digest in rows}
    return ({p for p, d in rows if source_hashes.get(p) != d}
            | (set(source_hashes) - set(stored)))

def closure_paths(prev: sqlite3.Connection, changed: set[str]) -> set[str]:
    """Docs mentioning any symbol DEFINED in `changed` docs. Outer join
    is role-agnostic: any mention of a changed-file symbol stales a doc."""
    if not changed:
        return set()
    marks = ",".join("?" * len(changed))
    ids = [r[0] for r in prev.execute(
        f"SELECT id FROM documents WHERE relative_path IN ({marks})",
        tuple(changed))]
    if not ids:
        return set()
    id_marks = ",".join("?" * len(ids))
    rows = prev.execute(
        f"SELECT DISTINCT c.document_id FROM mentions m "
        f"JOIN chunks c ON c.id = m.chunk_id "
        f"WHERE m.symbol_id IN ("
        f"  SELECT m2.symbol_id FROM mentions m2 "
        f"  JOIN chunks c2 ON c2.id = m2.chunk_id "
        f"  WHERE m2.role = 1 AND c2.document_id IN ({id_marks})"
        f") AND c.document_id NOT IN ({id_marks})",
        tuple(ids) * 2).fetchall()
    return {
        prev.execute("SELECT relative_path FROM documents WHERE id = ?",
                     (doc_id,)).fetchone()[0]
        for doc_id, in rows
    }

def decide(meta: IndexMetadata | None, prev_doc_count: int,
           changed_count: int, closure_count: int) -> CarryDecision:
    if meta is None:
        return CarryDecision(False, "no-previous", None)
    if meta.scip_mode not in CARRYABLE_STATES:
        return CarryDecision(False, "not-carryable", meta)
    if meta.carry_depth >= CARRY_DEPTH_CAP:
        return CarryDecision(False, "depth-cap", meta)
    if prev_doc_count and (changed_count + closure_count) / prev_doc_count > CARRY_CLOSURE_THRESHOLD:
        return CarryDecision(False, "threshold", meta)
    return CarryDecision(True, None, meta)

def build_carried_snapshot(prev_db: Path, dest: Path, drop: set[str],
                           prev_sha: str) -> None:
    """Filtered copy of prev_db minus `drop` docs, labeled carried.
    FK-safe order: mentions -> chunks -> defn_enclosing_ranges -> documents.
    global_symbols retained (orphans included). Counters recomputed from
    surviving mentions; calls/types -> 0 (only mentions are verifiable)."""
    shutil.copyfile(prev_db, dest)
    conn = sqlite3.connect(dest)
    try:
        if drop:
            marks = ",".join("?" * len(drop))
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM documents WHERE relative_path IN ({marks})",
                tuple(drop))]
            if ids:
                id_marks = ",".join("?" * len(ids))
                args = tuple(ids)
                conn.execute(
                    f"DELETE FROM mentions WHERE chunk_id IN ("
                    f"SELECT id FROM chunks WHERE document_id IN ({id_marks}))", args)
                conn.execute(f"DELETE FROM chunks WHERE document_id IN ({id_marks})", args)
                conn.execute(
                    f"DELETE FROM defn_enclosing_ranges WHERE document_id IN ({id_marks})",
                    args)
                conn.execute(f"DELETE FROM documents WHERE id IN ({id_marks})", args)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS carried_documents ("
            "document_id INTEGER PRIMARY KEY REFERENCES documents(id), "
            "carried_from_sha TEXT NOT NULL)")
        conn.execute("DELETE FROM carried_documents")
        conn.execute("INSERT INTO carried_documents SELECT id, ? FROM documents",
                     (prev_sha,))
        conn.execute(
            "UPDATE jarvis_snapshot SET scip_state = 'carried', "
            "scip_outlines = (SELECT COUNT(*) FROM documents), "
            "scip_definitions = (SELECT COUNT(*) FROM mentions WHERE role = 1), "
            "scip_references = (SELECT COUNT(*) FROM mentions WHERE role = 0), "
            "scip_calls = 0, scip_types = 0")
        conn.commit()
    finally:
        conn.close()
```
- [ ] **Step 5: Verify pass** — run `uv run pytest tests/test_carry.py -v` → PASS.
- [ ] **Step 6: Commit**

`git add src/jarvis/carry.py tests/fixtures/carry_db.py tests/test_carry.py` → commit `feat(index): add scip carry decision closure and filtered copy`

### Task 4: No-op guard (B)

**Files:** Modify `src/jarvis/index_cli.py` (helper near `_publish_atomically`; call in `_index_repo_locked` after stage 2, before SCIP suppression/attempt); test `tests/test_index_cli.py`.

**Interfaces:** Consumes `carry.published_metadata` (Task 3), `registry.get(slug).status`, stage-2 `manifest.source_hash`.
- Produces: `_noop_guard_hit(registry, slug: str, sha: str, source_hash: str, slug_dir: Path) -> bool`; `_index_repo_locked` returns `"unchanged"` on a hit (CLI prints an unchanged status line; no registry write, no pointer flip).
- [ ] **Step 1: Failing tests** — append to `tests/test_index_cli.py`:
```python
def _publish_fake_snapshot(slug_dir, sha="abc", source_hash="shash"):
    import json
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / f"index-{sha}-1.db").touch()
    (slug_dir / f"index-{sha}-1.metadata.json").write_text(json.dumps({
        "project": "_", "repo": "demo", "branch": "_",
        "commit_sha": sha, "source_hash": source_hash}))
    (slug_dir / "current").write_text(f"index-{sha}-1.db")

def test_noop_guard_contract(tmp_path):
    from jarvis import index_cli
    from jarvis.registry import Registry
    slug_dir = tmp_path / "slug"
    _publish_fake_snapshot(slug_dir)
    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("demo", "/tmp/demo", "python", "abc", "indexed")
        assert index_cli._noop_guard_hit(registry, "demo", "abc", "shash", slug_dir)
        assert not index_cli._noop_guard_hit(registry, "demo", "abc", "OTHER", slug_dir)
        assert not index_cli._noop_guard_hit(registry, "demo", "def", "shash", slug_dir)
        registry.upsert("demo", "/tmp/demo", "python", "abc", "failed",
                        status_origin="index", status_reason="boom")
        assert not index_cli._noop_guard_hit(registry, "demo", "abc", "shash", slug_dir)
        assert not index_cli._noop_guard_hit(registry, "demo", "abc", "shash",
                                             tmp_path / "missing")
    finally:
        registry.close()

def test_index_repo_noop_returns_unchanged(tmp_path, monkeypatch):
    """Pipeline-level: guard fires before SCIP; registry write count is 0."""
    from jarvis import index_cli
    scip_calls = []
    monkeypatch.setattr(index_cli, "_attempt_scip",
                        lambda *a, **k: scip_calls.append(1) or (_ for _ in ()).throw(
                            AssertionError("SCIP ran on a no-op")))
    # Reuse the repo's existing mini-git fixture helper (the same one other
    # pipeline tests in this file use) to create repo + registry + snapshot:
    # repo = <fixture mini git repo>; publish snapshot with sha=HEAD and
    # source_hash matching a prior capture; then:
    result = index_cli.index_repo(repo, slug="demo")  # adapt to real signature
    assert result == "unchanged"
    assert scip_calls == []
```
Second test: copy the nearest existing `index_repo` test's setup (mini-git fixture + `JARVIS_DATA_DIR` monkeypatch + faked zoekt), pre-publish a matching snapshot via `_publish_fake_snapshot`, assert `result == "unchanged"`, no SCIP call, and no new `index-*-2.db`.
- [ ] **Step 2: Verify failure** — run `uv run pytest tests/test_index_cli.py -k "noop" -v` → FAIL (`_noop_guard_hit` missing).
- [ ] **Step 3: Implement**
```python
def _noop_guard_hit(registry, slug: str, sha: str, source_hash: str,
                    slug_dir: Path) -> bool:
    """True when HEAD + source content + registry status are unchanged.
    On a hit the caller returns success with ZERO registry/pointer writes."""
    from jarvis.carry import published_metadata
    meta = published_metadata(slug_dir)
    if meta is None or meta.commit_sha != sha or meta.source_hash != source_hash:
        return False
    entry = registry.get(slug)
    return entry is not None and entry.status == "indexed"
```
In `_index_repo_locked`, immediately after stage 2 yields `manifest` (and before the SCIP suppression/attempt block — locate the exact post-syntax point by the `manifest` first becoming available):
```python
    if _noop_guard_hit(registry, slug, sha, manifest.source_hash,
                       config.index_dir(slug, root)):
        registry.close()
        return "unchanged"
```
`config.index_dir(slug, root)` is stage 7's directory — confirm its real signature at the call site. CLI/watch prints `[index] demo unchanged` for that return.
- [ ] **Step 4: Verify pass + gate** — run `uv run pytest tests/test_index_cli.py -k noop -v && uv run pytest -m "not integration" -rs` → PASS.
- [ ] **Step 5: Commit**

`git add src/jarvis/index_cli.py tests/test_index_cli.py` → commit `feat(cli): skip identical reindexes with a no-op guard`

### Task 5: Carry wiring (A) — watch path + metadata + stage fields

**Files:** Modify `src/jarvis/index_cli.py` (extract `_terminal_stage_fields` + `_publish_metadata_payload`; stage-3 carry branch; helper `_manifest_file_hashes`); test `tests/test_index_cli.py`.

**Interfaces:** Consumes Task 3 `carry` API; Task 1 `ScipStageFields(..., carried_from_sha, carry_depth)`; stage-2 manifest.
- Produces: · `_terminal_stage_fields(attempt: ScipAttempt|None, scip_enabled: bool, sha: str, carry: dict|None) -> ScipStageFields` · `_publish_metadata_payload(*, slug, sha, published_at, generation, source_hash, scip_mode, carried_from_sha, carry_depth) -> dict` · `_manifest_file_hashes(manifest) -> dict[str, str]` = `{captured.file_path: captured.file_hash}` — verify the manifest dataclass's exact attribute names first · On a watch carry: `attempt = ScipAttempt("carried", db_path=scratch/"carried.db")`; `carry_ctx = {"carried_from_sha": prev_origin_sha, "carry_depth": prev_depth}` where `prev_origin_sha = prev_meta.carried_from_sha or prev_meta.commit_sha`.
- [ ] **Step 1: Failing tests** — append to `tests/test_index_cli.py`:
```python
def test_terminal_stage_fields_carried():
    from jarvis.index_cli import ScipAttempt, _terminal_stage_fields
    stage = _terminal_stage_fields(
        ScipAttempt("carried"), True, "new111",
        carry={"carried_from_sha": "prev000", "carry_depth": 2})
    assert (stage.state, stage.carried_from_sha, stage.carry_depth) == \
        ("carried", "prev000", 3)  # depth increments on each carry

def test_terminal_stage_fields_available_resets_carry():
    from jarvis.index_cli import ScipAttempt, _terminal_stage_fields
    stage = _terminal_stage_fields(ScipAttempt("available"), True, "s", carry=None)
    assert (stage.state, stage.carried_from_sha, stage.carry_depth) == \
        ("available", None, 0)

def test_publish_metadata_payload_carry_fields():
    from jarvis.index_cli import _publish_metadata_payload
    p = _publish_metadata_payload(
        slug="demo", sha="new111", published_at="t", generation="g1",
        source_hash="sh", scip_mode="carried",
        carried_from_sha="prev000", carry_depth=1)
    assert p["scip_mode"] == "carried"
    assert p["carried_from_sha"] == "prev000"
    assert p["carry_depth"] == 1
    full = _publish_metadata_payload(
        slug="demo", sha="s", published_at="t", generation="g",
        source_hash="sh", scip_mode="available",
        carried_from_sha=None, carry_depth=0)
    assert full["scip_mode"] == "available"
```
- [ ] **Step 2: Verify failure** — run `uv run pytest tests/test_index_cli.py -k "terminal_stage or metadata_payload" -v` → FAIL.
- [ ] **Step 3: Implement**
```python
def _terminal_stage_fields(attempt, scip_enabled: bool, sha: str,
                           carry: dict | None) -> "ScipStageFields":
    """Post-publish stage decision, extracted for testability. Carried
    branch records prev depth + 1; metadata/registry report prev+1."""
    from jarvis.registry import ScipStageFields
    if attempt is not None and attempt.state == "available":
        return ScipStageFields(state="available")
    if attempt is not None and attempt.state == "carried":
        return ScipStageFields(
            state="carried",
            carried_from_sha=(carry or {}).get("carried_from_sha"),
            carry_depth=int((carry or {}).get("carry_depth") or 0) + 1,
        )
    if attempt is not None:
        return ScipStageFields(
            state=attempt.state, failure_reason=attempt.reason,
            failure_stderr=attempt.stderr, failed_at_sha=sha)
    if not scip_enabled:
        return ScipStageFields(state="disabled")
    return ScipStageFields(state="unavailable")

def _publish_metadata_payload(*, slug: str, sha: str, published_at: str,
                              generation: str, source_hash: str, scip_mode: str,
                              carried_from_sha: str | None, carry_depth: int) -> dict:
    return {
        "project": config.PROJECT, "repo": slug, "branch": config.BRANCH,
        "commit_sha": sha, "published_at": published_at,
        "generation": generation, "source_hash": source_hash,
        "scip_mode": scip_mode, "carried_from_sha": carried_from_sha,
        "carry_depth": carry_depth,
    }
```
Replace the inline terminal-stage `if/elif` with `_terminal_stage_fields(...)` (keep every existing branch; only `carried` is new) and stage 7's inline `json.dumps` with the payload helper, computing:
```python
    scip_mode = attempt.state if attempt is not None else "unavailable"
    carried_sha = carry_ctx["carried_from_sha"] if carry_ctx else None
    carry_depth = (carry_ctx["carry_depth"] + 1) if carry_ctx else 0
```
Stage-3 branch in `_index_repo_locked` (after the no-op guard; `suppressed` is the existing watch-suppression flag):
```python
    carry_ctx: dict | None = None
    if (watch and scip_enabled and resolved_language in _INDEXER_BY_LANGUAGE
            and not suppressed):
        from jarvis import carry as carry_mod
        slug_dir = config.index_dir(slug, root)
        prev_meta = carry_mod.published_metadata(slug_dir)
        prev_name = carry_mod.pointer_target(slug_dir)
        prev_db = slug_dir / prev_name if prev_name else None
        if prev_meta is not None and prev_db is not None and prev_db.exists():
            try:
                prev_conn = sqlite3.connect(
                    f"file:{prev_db}?mode=ro&immutable=1", uri=True)
                try:
                    doc_count = prev_conn.execute(
                        "SELECT COUNT(*) FROM documents").fetchone()[0]
                    changed = carry_mod.changed_paths(
                        prev_conn, _manifest_file_hashes(manifest))
                    closure = carry_mod.closure_paths(prev_conn, changed)
                finally:
                    prev_conn.close()
                decision = carry_mod.decide(
                    prev_meta, doc_count, len(changed), len(closure))
                if decision.carry:
                    carried_db = scratch / "carried.db"
                    prev_origin = (prev_meta.carried_from_sha
                                   or prev_meta.commit_sha)
                    carry_mod.build_carried_snapshot(
                        prev_db, carried_db, changed | closure, prev_origin)
                    attempt = ScipAttempt("carried", db_path=carried_db)
                    carry_ctx = {"carried_from_sha": prev_origin,
                                 "carry_depth": prev_meta.carry_depth}
            except sqlite3.Error:
                attempt = None  # unreadable previous snapshot → full run
    if attempt is None or attempt.state != "carried":
        attempt = _attempt_scip(repo_path, resolved_language, scheme, slug, scratch)
```
Initialize `attempt = None` before the block (guard/suppression keep today's semantics). A `failed` full attempt while a carried snapshot is current leaves the pointer untouched — existing stage-7 behavior.
- [ ] **Step 4: Verify pass + gate** — run `uv run pytest tests/test_index_cli.py -k "terminal_stage or metadata_payload" -v && uv run pytest -m "not integration" -rs` → PASS.
- [ ] **Step 5: Commit**

`git add src/jarvis/index_cli.py tests/test_index_cli.py` → commit `feat(cli): carry forward scip rows on watch reindexes`

### Task 6: Query provenance

**Files:** Modify `src/jarvis/query.py` (`FreshnessSnapshot` ~L65; `_freshness_snapshot` ~L77; `Location`; result-mapping sites); test `tests/test_query.py`.

**Interfaces:** Consumes `IndexMetadata.carried_from_sha` (Task 2); `carried_documents` sidecar (Task 3).
- Produces: `FreshnessSnapshot.scip_carried_from_sha: str|None = None`; `Location.carried_from_sha: str|None = None`; helper `_carried_sha_map(conn: sqlite3.Connection) -> dict[int, str]` — `{}` when the table is absent (full runs / old snapshots).
- [ ] **Step 1: Failing tests** — append to `tests/test_query.py`:
```python
def test_freshness_snapshot_exposes_carried_sha():
    from jarvis.index_reader import IndexMetadata
    from jarvis.query import _freshness_snapshot
    snap = _freshness_snapshot(IndexMetadata(
        project="_", repo="d", branch="_", commit_sha="abc",
        scip_mode="carried", carried_from_sha="prev000", carry_depth=1))
    assert snap.scip_carried_from_sha == "prev000"

def test_freshness_snapshot_none_when_full():
    from jarvis.index_reader import IndexMetadata
    from jarvis.query import _freshness_snapshot
    snap = _freshness_snapshot(IndexMetadata(
        project="_", repo="d", branch="_", commit_sha="abc", scip_mode="available"))
    assert snap.scip_carried_from_sha is None

def test_carried_sha_map_reads_sidecar(tmp_path):
    import sqlite3
    from tests.fixtures.carry_db import build_prev_snapshot
    from jarvis.carry import build_carried_snapshot
    from jarvis.query import _carried_sha_map
    build_prev_snapshot(tmp_path / "prev.db")
    build_carried_snapshot(tmp_path / "prev.db", tmp_path / "c.db",
                           drop={"a.py"}, prev_sha="prev000")
    conn = sqlite3.connect(tmp_path / "c.db")
    try:
        assert _carried_sha_map(conn) == {2: "prev000"}  # b.py doc id
        conn.execute("DROP TABLE carried_documents")
        assert _carried_sha_map(conn) == {}
    finally:
        conn.close()
```
- [ ] **Step 2: Verify failure** — run `uv run pytest tests/test_query.py -k carried -v` → FAIL.
- [ ] **Step 3: Implement**
```python
# FreshnessSnapshot — append after generation:
    # Snapshot-level SCIP carry provenance; None on full runs.
    scip_carried_from_sha: str | None = None

# _freshness_snapshot(): pass metadata.carried_from_sha through.

# Location — append the same default-None field.

def _carried_sha_map(conn: sqlite3.Connection) -> dict[int, str]:
    """document_id -> carried_from_sha from the sidecar; {} when absent."""
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='carried_documents'"
    ).fetchone()
    if has is None:
        return {}
    return {doc: sha for doc, sha in conn.execute(
        "SELECT document_id, carried_from_sha FROM carried_documents")}
```
At each `Location`-building site (definitions, references, calls, types), read `_carried_sha_map(conn)` once per query and set the field when the document id is present. Syntax-lane Locations stay `None`.
- [ ] **Step 4: Verify pass** — run `uv run pytest tests/test_query.py -v` → PASS.
- [ ] **Step 5: Commit**

`git add src/jarvis/query.py tests/test_query.py` → commit `feat(query): label carried scip results with source sha`

### Task 7: `getIndexStatus` fields + integration test

**Files:** Modify `src/jarvis/server.py` (`get_index_status` ~L601; new `_carried_document_count`); tests `tests/test_server.py`, `tests/test_index_cli.py` (integration).

**Interfaces:** Consumes `FreshnessSnapshot.scip_carried_from_sha` (Task 6); registry `scip_state`/`scip_carry_depth` (Task 1); current snapshot (Task 3 sidecar).
- Produces: payload keys `scipMode`, `carriedFromSha`, `carryDepth`, `carriedDocumentCount`, `fullRefreshReason`.
- [ ] **Step 1: Failing test** — append to `tests/test_server.py` (reuse this file's existing fake-service pattern):
```python
def test_get_index_status_includes_carry_fields(monkeypatch):
    from jarvis import server
    fake = server._FakeServiceForTests(  # extend the existing fake in this file
        indexed=True,
        freshness=server._FakeFreshnessForTests(
            stale=False, scip_carried_from_sha="prev000"))
    monkeypatch.setattr(server, "_service", lambda: fake)
    payload = server.get_index_status("demo")
    assert payload["scipMode"] == "carried"
    assert payload["carriedFromSha"] == "prev000"
    assert isinstance(payload["carryDepth"], int)
    assert isinstance(payload["carriedDocumentCount"], int)
    assert "fullRefreshReason" in payload
```
Extend the existing fake classes in that file with `scip_carried_from_sha`; no new fakes module.
- [ ] **Step 2: Verify failure** — run `uv run pytest tests/test_server.py -k carry -v` → FAIL.
- [ ] **Step 3: Implement** — in `get_index_status`, after `_freshness_fields(freshness)`:
```python
    registry_entry = _registry_entry(repo)  # existing lookup used by _registry_status
    carried = freshness.scip_carried_from_sha
    carry_fields = {
        "scipMode": "carried" if carried else (registry_entry.scip_state
                                               if registry_entry else "unknown"),
        "carriedFromSha": carried,
        "carryDepth": registry_entry.scip_carry_depth if registry_entry else 0,
        "carriedDocumentCount": _carried_document_count(repo),
        "fullRefreshReason": (registry_entry.status_reason
                              if registry_entry and carried else None),
    }
```
Add `_carried_document_count(repo) -> int`: open the current snapshot `mode=ro&immutable=1`, count `carried_documents`; return `0` when pointer/table/db absent (reuse the module's snapshot-open helper; never raise).
- [ ] **Step 4: Integration test** — append to `tests/test_index_cli.py`:
```python
@pytest.mark.integration
def test_watch_carry_then_depth_cap_full_reset(tmp_path, monkeypatch):
    import shutil
    import sqlite3 as sq
    if not all(shutil.which(b) for b in ("scip-python", "scip", "zoekt-git-index")):
        pytest.skip("scip/zoekt toolchain absent")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"; repo.mkdir()
    import subprocess
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                    capture_output=True, text=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (repo / "a.py").write_text("class A:\n    def f(self):\n        return 1\n")
    (repo / "b.py").write_text("from a import A\nA().f()\n")
    git("add", "."); git("commit", "-qm", "initial")
    from jarvis import index_cli
    rc = index_cli.index_repo(repo, slug="carrydemo")  # adapt to real signature
    reg = index_cli.Registry(tmp_path / "data" / "registry.db")
    assert reg.get("carrydemo").scip_state == "available"
    assert reg.get("carrydemo").scip_carry_depth == 0
    reg.close()
    (repo / "b.py").write_text("from a import A\nA().f()\n# touched\n")
    rc = index_cli.index_repo(repo, slug="carrydemo", watch=True)
    reg = index_cli.Registry(tmp_path / "data" / "registry.db")
    row = reg.get("carrydemo")
    assert (row.scip_state, row.scip_carry_depth) == ("carried", 1)
    assert row.scip_carried_from_sha is not None
    reg.close()
    slug_dir = tmp_path / "data" / "scip" / "_" / "carrydemo" / "_"
    pointer = (slug_dir / "current").read_text().strip()
    conn = sq.connect(f"file:{slug_dir / pointer}?mode=ro&immutable=1", uri=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM carried_documents").fetchone()[0] >= 1
        assert {r[0] for r in conn.execute(
            "SELECT relative_path FROM documents")} == {"a.py"}
    finally:
        conn.close()
    # Depth cap: five more carry-depth generations force a full run.
    for i in range(5):
        (repo / "b.py").write_text(f"# touched {i}\n")
        git("add", "."); git("commit", "-qm", f"t{i}")
        index_cli.index_repo(repo, slug="carrydemo", watch=True)
    reg = index_cli.Registry(tmp_path / "data" / "registry.db")
    assert (reg.get("carrydemo").scip_state,
            reg.get("carrydemo").scip_carry_depth) == ("available", 0)
    reg.close()
```
`index_repo` signature: `(repo_path, *, slug=None, root=None, scheme=None, semantic_include=None, language=None, scip=None, semantic=True, watch=False) -> str`. Copy lock handling from the nearest pipeline test if needed.
- [ ] **Step 5: Run gates** — run `uv run pytest -m "not integration" -rs && uv run python scripts/check_versions.py` → PASS. If toolchain present: `uv run pytest tests/test_index_cli.py::test_watch_carry_then_depth_cap_full_reset -m integration -v`.
- [ ] **Step 6: Commit**

`git add src/jarvis/server.py tests/test_server.py tests/test_index_cli.py` → commit `feat(server): expose scip carry provenance in index status`
