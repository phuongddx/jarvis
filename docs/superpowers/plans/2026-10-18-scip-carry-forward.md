# SCIP Carry-Forward Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Watch runs reuse the previous generation's converted SCIP data for every document outside the edit's reverse-symbol closure, instead of re-running the whole-project indexer.

**Architecture:** A new pure module `jarvis/carry.py` computes the changed-path set, the invalidated-document closure, and performs the row copy into the new snapshot. `index_cli.py`'s stage 3 becomes a three-way choice (carry / escalate-to-full / full). Registry, snapshot facts, and metadata gain a `carried` state and `scip_carried_from_sha`; freshness reports `scip_as_of`.

**Tech Stack:** Python 3.12+, stdlib `sqlite3` only. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-18-scip-carry-forward-design.md`

## Global Constraints

- Python 3.12+ syntax only: `X | None`, builtin generics, frozen dataclasses, no async, no ORM.
- Raw stdlib `sqlite3` with parameterized SQL only — never f-string user *values* into SQL (f-string placeholder lists built from `?` count are the established pattern).
- Typed exceptions live in their owning module.
- Unit gate: `uv run pytest -m "not integration" -rs` must pass after every task.
- Lowercase imperative Conventional Commits (`feat(index): …`, `test(carry): …`).
- Tests mirror modules: `tests/test_<module>.py`; state-touching tests set `JARVIS_DATA_DIR` to `tmp_path`.
- Line anchors below are accurate as of commit `30f0ee9`; they drift as earlier tasks land — locate blocks by their quoted code, not only by number.

---

### Task 1: `carry.py` — diff + closure planning

**Files:**
- Create: `src/jarvis/carry.py`
- Test: `tests/test_carry.py`

**Interfaces:**
- Consumes: `jarvis.syntax_index.SourceManifest` (fields `files: tuple[CapturedFile, ...]`, each `CapturedFile.file_path: str`, `.file_hash: str | None`, `.in_scope: bool`); `jarvis.scip_decoder.SymbolRoles.DEFINITION`.
- Produces: `CarryPlan(invalidated: frozenset[int], total_documents: int)`; `changed_paths(previous: sqlite3.Connection, manifest: SourceManifest) -> set[str]`; `plan_carry(previous: sqlite3.Connection, changed: set[str]) -> CarryPlan`; `should_escalate(plan: CarryPlan) -> bool`; plus `carried_from_sha(previous: sqlite3.Connection) -> str | None` (used by Tasks 2 and 4).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_carry.py`:

```python
"""Unit tests for jarvis.carry: diff, closure, escalation threshold."""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.carry import (
    CARRY_ESCALATION_THRESHOLD,
    CarryPlan,
    carried_from_sha,
    changed_paths,
    plan_carry,
    should_escalate,
)
from jarvis.syntax_index import CapturedFile, SourceManifest


def _captured(path: str, file_hash: str) -> CapturedFile:
    return CapturedFile(
        file_path=path, language="python", file_hash=file_hash,
        source_path=None, in_scope=True, reason=None,
    )


def _make_db(tmp_path, *, docs: dict[str, int], defs: dict[str, list[str]],
             refs: dict[str, list[str]], syntax_hashes: dict[str, str]):
    """docs: relative_path -> id. defs: doc path -> symbols DEFINED there
    (id = 1000 + symbol index). refs: doc path -> symbol ids mentioned.
    syntax_hashes: file_path -> hash for the diff side."""
    conn = sqlite3.connect(tmp_path / "previous.db")
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, language TEXT,
            relative_path TEXT NOT NULL UNIQUE, position_encoding TEXT, text TEXT);
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL, start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL, occurrences BLOB NOT NULL);
        CREATE TABLE mentions (
            chunk_id INTEGER NOT NULL, symbol_id INTEGER NOT NULL, role INTEGER NOT NULL,
            PRIMARY KEY (chunk_id, symbol_id, role));
        CREATE TABLE global_symbols (
            id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE, display_name TEXT,
            kind INTEGER, documentation TEXT, signature BLOB, enclosing_symbol TEXT,
            relationships BLOB);
        CREATE TABLE defn_enclosing_ranges (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, symbol_id INTEGER NOT NULL,
            start_line INTEGER, start_char INTEGER, end_line INTEGER, end_char INTEGER);
        CREATE TABLE syntax_files (
            file_path TEXT PRIMARY KEY, language TEXT, file_hash TEXT,
            parser_identity TEXT, in_scope INTEGER, state TEXT, reason TEXT,
            scip_outline INTEGER DEFAULT 0, scip_definition INTEGER DEFAULT 0);
        """
    )
    chunk_of_doc: dict[int, int] = {}
    for path, doc_id in docs.items():
        chunk_id = doc_id  # one chunk per doc, same id for simplicity
        chunk_of_doc[doc_id] = chunk_id
        conn.execute(
            "INSERT INTO chunks (id, document_id, chunk_index, start_line, end_line, occurrences)"
            " VALUES (?, ?, 0, 1, 2, x'00')", (chunk_id, doc_id))
    for i, symbol in enumerate(sorted(set(sum(defs.values(), [])))):
        conn.execute(
            "INSERT INTO global_symbols (id, symbol) VALUES (?, ?)", (1000 + i, symbol))
    symbol_id = {s: 1000 + i for i, s in enumerate(sorted(set(sum(defs.values(), []))))}
    for path, symbols in defs.items():
        for symbol in symbols:
            conn.execute(
                "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?, ?, ?)",
                (chunk_of_doc[docs[path]], symbol_id[symbol], 1))  # 1 = DEFINITION
    for path, symbols in refs.items():
        for symbol in symbols:
            conn.execute(
                "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?, ?, ?)",
                (chunk_of_doc[docs[path]], symbol_id[symbol], 0))  # 0 = reference
    for path, h in syntax_hashes.items():
        conn.execute(
            "INSERT INTO syntax_files (file_path, language, file_hash, in_scope, state)"
            " VALUES (?, 'python', ?, 1, 'parsed')", (path, h))
    conn.commit()
    return conn


def _fixture(tmp_path):
    """A defines `symA`; B references `symA`; C references only `symC`."""
    return _make_db(
        tmp_path,
        docs={"src/a.py": 1, "src/b.py": 2, "src/c.py": 3},
        defs={"src/a.py": ["symA"], "src/c.py": ["symC"]},
        refs={"src/b.py": ["symA"]},
        syntax_hashes={"src/a.py": "h1", "src/b.py": "h2", "src/c.py": "h3"},
    )


def test_changed_paths_detects_modified_new_and_removed(tmp_path):
    conn = _fixture(tmp_path)
    manifest = SourceManifest(
        files=(_captured("src/a.py", "h1-changed"),   # modified
               _captured("src/new.py", "h9"),          # new
               _captured("src/c.py", "h3")),           # unchanged; src/b.py removed
        source_hash="x",
    )
    assert changed_paths(conn, manifest) == {"src/a.py", "src/new.py", "src/b.py"}


def test_closure_invalidates_changed_docs_and_mentioners_only(tmp_path):
    conn = _fixture(tmp_path)
    plan = plan_carry(conn, {"src/a.py"})
    assert plan.total_documents == 3
    assert plan.invalidated == frozenset({1, 2})  # a.py changed, b.py mentions symA; c.py untouched


def test_removed_file_counts_as_changed_for_closure(tmp_path):
    conn = _fixture(tmp_path)
    # c.py removed -> its doc defines symC, but nobody references symC: only c.py invalidates
    plan = plan_carry(conn, {"src/c.py"})
    assert plan.invalidated == frozenset({3})


def test_empty_diff_invalidates_nothing(tmp_path):
    conn = _fixture(tmp_path)
    assert plan_carry(conn, set()).invalidated == frozenset()


def test_changed_path_without_scip_document_is_harmless(tmp_path):
    conn = _fixture(tmp_path)
    plan = plan_carry(conn, {"docs/readme.md"})
    assert plan.invalidated == frozenset()


def test_should_escalate_at_80_percent(tmp_path):
    below = CarryPlan(invalidated=frozenset(range(7)), total_documents=10)   # 70%
    at = CarryPlan(invalidated=frozenset(range(8)), total_documents=10)      # 80%
    assert should_escalate(below) is False
    assert should_escalate(at) is True
    assert should_escalate(CarryPlan(invalidated=frozenset({1}), total_documents=0)) is False


def test_should_escalate_uses_stable_baseline_across_chained_carries():
    # A previous carry already dropped 5 of a 10-doc baseline; 5 survive,
    # this run invalidates 3 more -> cumulative 8/10 >= 80% -> escalate.
    chained = CarryPlan(invalidated=frozenset({1, 2, 3}), total_documents=5,
                        baseline_documents=10)
    assert should_escalate(chained) is True
    # Against the shrunk denominator the same run would NOT trip — the
    # exact decay hole the baseline exists to close.
    assert len(chained.invalidated) < CARRY_ESCALATION_THRESHOLD * chained.total_documents


def test_carried_from_sha_coalesces_chained_carries(tmp_path):
    conn = _fixture(tmp_path)
    conn.execute(
        "CREATE TABLE jarvis_snapshot (singleton INTEGER PRIMARY KEY,"
        " commit_sha TEXT, scip_carried_from_sha TEXT)")
    conn.execute("INSERT INTO jarvis_snapshot VALUES (1, 'watch2', 'origfull')")
    conn.commit()
    assert carried_from_sha(conn) == "origfull"   # previous was carried: propagate orig
    conn.execute("UPDATE jarvis_snapshot SET scip_carried_from_sha = NULL")
    conn.commit()
    assert carried_from_sha(conn) == "watch2"     # previous was a full run
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_carry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jarvis.carry'`

- [ ] **Step 3: Write `src/jarvis/carry.py`**

```python
"""Watch-run SCIP carry-forward: diff, closure, escalation (design:
docs/superpowers/specs/2026-10-18-scip-carry-forward-design.md).

Every function here reads the PREVIOUS published snapshot (read-only) and
writes only the scratch snapshot being assembled. Nothing in this module
runs an indexer. Kept free of index_cli imports so the pipeline can import
it without a cycle.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from jarvis.scip_decoder import SymbolRoles
from jarvis.syntax_index import SourceManifest

__all__ = [
    "CARRY_ESCALATION_THRESHOLD",
    "CarryPlan",
    "apply_carry",
    "carried_from_sha",
    "changed_paths",
    "plan_carry",
    "should_escalate",
]

# Share of previous SCIP documents at (or above) which a watch run
# escalates to a full indexer pass instead of carrying (design §8).
CARRY_ESCALATION_THRESHOLD = 0.8


@dataclass(frozen=True)
class CarryPlan:
    """Which previous SCIP documents survive into the next generation."""

    invalidated: frozenset[int]  # previous `documents.id` values dropped
    total_documents: int         # documents in the previous snapshot
    # Denominator that does NOT decay across chained carries: the document
    # count at the last FULL SCIP run. None on hand-built plans (tests) —
    # `should_escalate` then falls back to `total_documents`.
    baseline_documents: int | None = None


def changed_paths(previous: sqlite3.Connection, manifest: SourceManifest) -> set[str]:
    """Repo paths whose content differs from the previous snapshot, plus
    new and removed paths. Old hashes come from the previous snapshot's
    `syntax_files` (written by the same manifest hash at publish time).
    Only in-scope rows compare: out-of-scope paths never have SCIP
    documents, and counting them as 'removed' every run is noise."""
    old = dict(previous.execute(
        "SELECT file_path, file_hash FROM syntax_files WHERE in_scope = 1"))
    new_paths = {c.file_path for c in manifest.files if c.in_scope}
    changed = {p for p in old if p not in new_paths}
    for captured in manifest.files:
        if captured.in_scope and old.get(captured.file_path) != captured.file_hash:
            changed.add(captured.file_path)
    return changed


def plan_carry(previous: sqlite3.Connection, changed: set[str]) -> CarryPlan:
    """Invalidated = documents at changed paths ∪ documents mentioning any
    symbol DEFINED in a changed document (the closure, design §5)."""
    total = previous.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    baseline = _carry_baseline(previous) or total
    if not changed:
        return CarryPlan(invalidated=frozenset(), total_documents=total,
                         baseline_documents=baseline)

    path_ph = ",".join("?" for _ in changed)
    changed_docs = frozenset(
        row[0]
        for row in previous.execute(
            f"SELECT id FROM documents WHERE relative_path IN ({path_ph})",
            tuple(changed),
        )
    )
    if not changed_docs:
        return CarryPlan(invalidated=frozenset(), total_documents=total,
                         baseline_documents=baseline)

    doc_ph = ",".join("?" for _ in changed_docs)
    closure = frozenset(
        row[0]
        for row in previous.execute(
            f"""
            WITH defd AS (
                SELECT DISTINCT m.symbol_id
                FROM mentions m JOIN chunks c ON m.chunk_id = c.id
                WHERE c.document_id IN ({doc_ph})
                  AND (m.role & ?) != 0
            )
            SELECT DISTINCT c.document_id
            FROM mentions m JOIN chunks c ON m.chunk_id = c.id
            WHERE m.symbol_id IN defd
            """,
            (*changed_docs, SymbolRoles.DEFINITION),
        )
    )
    return CarryPlan(
        invalidated=frozenset(changed_docs | closure), total_documents=total,
        baseline_documents=baseline,
    )


def should_escalate(plan: CarryPlan) -> bool:
    """True when cumulative invalidation covers >= 80% of the BASELINE —
    the document count at the last full SCIP run (design §8). Measuring
    against the baseline, not the previous (already shrunk) generation,
    means chained watch carries cannot decay coverage past the threshold
    without tripping it. Zero-baseline snapshots never escalate."""
    baseline = plan.baseline_documents if plan.baseline_documents is not None else plan.total_documents
    return baseline > 0 and (
        baseline - (plan.total_documents - len(plan.invalidated))
        >= CARRY_ESCALATION_THRESHOLD * baseline
    )


def _carry_baseline(previous: sqlite3.Connection) -> int | None:
    """The previous generation's `scip_carry_baseline` when it was itself
    carried (chained carry); None on a full-run snapshot."""
    try:
        columns = {row[1] for row in previous.execute(
            "PRAGMA table_info(jarvis_snapshot)")}
        if "scip_carry_baseline" not in columns:
            return None
        row = previous.execute(
            "SELECT scip_carry_baseline FROM jarvis_snapshot WHERE singleton = 1"
        ).fetchone()
    except sqlite3.OperationalError:  # legacy snapshot without the table
        return None
    return row[0] if row is not None and row[0] is not None else None


def carried_from_sha(previous: sqlite3.Connection) -> str | None:
    """The commit the previous snapshot's SCIP data was last FULLY indexed
    at — the value recorded as `scip_carried_from_sha` on a carried
    generation. COALESCE matters for chained carries: a previous carried
    generation's own `commit_sha` is the last watch commit, not the commit
    its SCIP data reflects."""
    try:
        columns = {row[1] for row in previous.execute(
            "PRAGMA table_info(jarvis_snapshot)")}
        expr = ("COALESCE(scip_carried_from_sha, commit_sha)"
                if "scip_carried_from_sha" in columns else "commit_sha")
        row = previous.execute(
            f"SELECT {expr} FROM jarvis_snapshot WHERE singleton = 1"
        ).fetchone()
    except sqlite3.OperationalError:  # legacy snapshot without the table
        return None
    return row[0] if row is not None else None
```

(`apply_carry` arrives in Task 2; do not reference it yet outside `__all__` — move its `__all__` entry to Task 2's diff.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_carry.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/carry.py tests/test_carry.py
git commit -m "feat(index): add scip carry diff and closure planning"
```

---

### Task 2: `apply_carry` — the snapshot merge

**Files:**
- Modify: `src/jarvis/carry.py`
- Test: `tests/test_carry.py`

**Interfaces:**
- Consumes: `CarryPlan` from Task 1.
- Produces: `apply_carry(dest: sqlite3.Connection, previous: sqlite3.Connection, plan: CarryPlan) -> int` — returns the number of documents carried. `dest` is a connection to this run's `syntax.db` (stage-2 output: has `syntax_files`/`syntax_symbols`, lacks the five SCIP tables).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_carry.py`:

```python
from jarvis.carry import apply_carry  # extend the Task 1 import


def _dest_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "dest.db")
    conn.executescript(
        """
        CREATE TABLE syntax_files (
            file_path TEXT PRIMARY KEY, language TEXT, file_hash TEXT,
            parser_identity TEXT, in_scope INTEGER, state TEXT, reason TEXT,
            scip_outline INTEGER DEFAULT 0, scip_definition INTEGER DEFAULT 0);
        """
    )
    return conn


def test_apply_carry_copies_uninvalidated_and_drops_invalidated(tmp_path):
    previous = _fixture(tmp_path)
    dest = _dest_conn(tmp_path)
    plan = plan_carry(previous, {"src/a.py"})  # invalidates docs 1, 2; carries doc 3

    carried = apply_carry(dest, previous, plan)

    assert carried == 1
    assert dest.execute(
        "SELECT relative_path FROM documents").fetchall() == [("src/c.py",)]
    # mentions reference only carried chunks/symbols: FK integrity by construction
    orphan_chunks = dest.execute(
        "SELECT COUNT(*) FROM mentions m LEFT JOIN chunks c ON m.chunk_id = c.id"
        " WHERE c.id IS NULL").fetchone()[0]
    orphan_symbols = dest.execute(
        "SELECT COUNT(*) FROM mentions m LEFT JOIN global_symbols g ON m.symbol_id = g.id"
        " WHERE g.id IS NULL").fetchone()[0]
    assert (orphan_chunks, orphan_symbols) == (0, 0)


def test_apply_carry_with_empty_plan_copies_everything(tmp_path):
    previous = _fixture(tmp_path)
    dest = _dest_conn(tmp_path)

    carried = apply_carry(dest, previous, plan_carry(previous, set()))

    assert carried == 3
    assert dest.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    assert dest.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == 3
    assert dest.execute("SELECT COUNT(*) FROM global_symbols").fetchone()[0] == 2


def test_apply_carry_is_idempotent_on_fresh_dest_only(tmp_path):
    """Guard the single-source precondition: applying twice to the same dest
    must fail loudly (UNIQUE violation), never silently duplicate."""
    previous = _fixture(tmp_path)
    dest = _dest_conn(tmp_path)
    apply_carry(dest, previous, plan_carry(previous, set()))
    with pytest.raises(sqlite3.IntegrityError):
        apply_carry(dest, previous, plan_carry(previous, set()))


def test_apply_carry_rows_are_verbatim(tmp_path):
    """Spec §12 AC4 proxy: a full carry copies every row byte-identically —
    same navigation answers by construction."""
    previous = _fixture(tmp_path)
    dest = _dest_conn(tmp_path)
    apply_carry(dest, previous, plan_carry(previous, set()))
    for table in ("documents", "chunks", "mentions", "global_symbols",
                  "defn_enclosing_ranges"):
        assert dest.execute(
            f"SELECT * FROM {table} ORDER BY 1").fetchall() == previous.execute(
            f"SELECT * FROM {table} ORDER BY 1").fetchall(), table
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_carry.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_carry'`

- [ ] **Step 3: Implement `apply_carry` in `src/jarvis/carry.py`**

Add below `should_escalate` (and it was already listed in `__all__`):

```python
# The five tables `scip expt-convert` produces, DDL matching the converter
# output byte-for-byte in column order — `apply_carry`'s positional INSERTs
# depend on it. Mentions precede global_symbols exactly as upstream emits
# them; sqlite resolves the forward reference at runtime and FK enforcement
# is off by default.
_SCIP_TABLES_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    language TEXT,
    relative_path TEXT NOT NULL UNIQUE,
    position_encoding TEXT,
    text TEXT
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    occurrences BLOB NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id)
);
CREATE INDEX idx_chunks_line_range ON chunks(document_id, start_line, end_line);
CREATE INDEX idx_chunks_doc_id ON chunks(document_id);
CREATE TABLE mentions (
    chunk_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    role INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, symbol_id, role),
    FOREIGN KEY (chunk_id) REFERENCES chunks(id),
    FOREIGN KEY (symbol_id) REFERENCES global_symbols(id)
);
CREATE INDEX idx_mentions_symbol_id_role ON mentions(symbol_id, role);
CREATE TABLE global_symbols (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    display_name TEXT,
    kind INTEGER,
    documentation TEXT,
    signature BLOB,
    enclosing_symbol TEXT,
    relationships BLOB
);
CREATE INDEX idx_global_symbols_symbol ON global_symbols(symbol);
CREATE TABLE defn_enclosing_ranges (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (symbol_id) REFERENCES global_symbols(id)
);
CREATE INDEX idx_defn_enclosing_ranges_symbol_id ON defn_enclosing_ranges(symbol_id);
CREATE INDEX idx_defn_enclosing_ranges_document ON defn_enclosing_ranges(document_id, start_line, end_line);
"""


def apply_carry(dest: sqlite3.Connection, previous: sqlite3.Connection,
                plan: CarryPlan) -> int:
    """Copy every non-invalidated SCIP document's rows from the previous
    snapshot into `dest`, creating the converter's five tables first.
    Primary keys are preserved verbatim — dest's SCIP tables hold carried
    rows from a single source, so ids cannot collide (design §6). Returns
    the number of documents carried."""
    dest.executescript(_SCIP_TABLES_DDL)

    carried_ids = frozenset(
        row[0]
        for row in previous.execute(
            "SELECT id FROM documents WHERE id NOT IN "
            f"({','.join('?' for _ in plan.invalidated) or 'NULL'})",
            tuple(plan.invalidated),
        )
    )

    def _copy(table: str, where: str, params: tuple) -> None:
        rows = previous.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
        if rows:
            marks = ",".join("?" for _ in rows[0])
            dest.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)

    doc_ph = ",".join("?" for _ in carried_ids) or "NULL"
    params = tuple(carried_ids)
    _copy("documents", f"id IN ({doc_ph})", params)
    _copy("chunks", f"document_id IN ({doc_ph})", params)
    _copy("mentions",
          f"chunk_id IN (SELECT id FROM chunks WHERE document_id IN ({doc_ph}))",
          params)
    _copy("defn_enclosing_ranges", f"document_id IN ({doc_ph})", params)
    # Referenced-by-carried-rows ONLY, and the reference set must come
    # from DEST (already copied), not previous: previous's mentions still
    # contain invalidated documents' rows, so selecting against them would
    # carry symbols nobody in the new generation references — stale
    # `relationships` rows would then surface in type-hierarchy scans
    # (query.py's full-table reads).
    referenced = [row[0] for row in dest.execute(
        "SELECT symbol_id FROM mentions"
        " UNION SELECT symbol_id FROM defn_enclosing_ranges")]
    sym_ph = ",".join("?" for _ in referenced) or "NULL"
    _copy("global_symbols", f"id IN ({sym_ph})", tuple(referenced))
    dest.commit()
    return len(carried_ids)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_carry.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/carry.py tests/test_carry.py
git commit -m "feat(index): add scip carry merge into fresh snapshot"
```

---

### Task 3: State plumbing — `carried` vocabulary, facts, metadata

**Files:**
- Modify: `src/jarvis/registry.py:42-44` (`SCIP_STATES`)
- Modify: `src/jarvis/syntax_index.py:355-369` (`jarvis_snapshot` DDL), `:644-687` (`finalize_snapshot`), `:136-140` (`SnapshotFacts`), `:732-755` (`_read_new_format_facts`)
- Modify: `src/jarvis/index_reader.py:44-59` (`IndexMetadata`), `:99-107` (`read_metadata`)
- Test: `tests/test_registry.py`, `tests/test_syntax_index.py` (the module where `finalize_snapshot` is tested today), `tests/test_query.py` (`read_metadata` coverage lives with freshness)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SCIP_STATES` includes `"carried"`; `finalize_snapshot(conn, *, generation, commit_sha, published_at, source_hash, scip_state, scip_carried_from_sha: str | None = None, scip_carry_baseline: int | None = None)`; `SnapshotFacts.scip_carried_from_sha: str | None = None` and `.scip_carry_baseline: int | None = None`; `IndexMetadata.scip_carried_from_sha: str | None = None`.

- [ ] **Step 1: Write the failing tests**

In the registry test file:

```python
def test_scip_stage_accepts_carried_state(tmp_path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("slug", "/repo", "python", "abc", "indexed",
               scip_stage=ScipStageFields(state="carried"))
    assert reg.get("slug").scip_state == "carried"
```

In the snapshot-facts test file:

```python
def test_finalize_snapshot_records_carried_from_sha(tmp_path):
    conn = _facts_fixture(tmp_path)  # below
    facts = finalize_snapshot(
        conn, generation="g", commit_sha="new", published_at="2026-10-18T00:00:00+00:00",
        source_hash="s", scip_state="carried", scip_carried_from_sha="old",
    )
    assert facts.scip_carried_from_sha == "old"
    row = conn.execute(
        "SELECT scip_carried_from_sha FROM jarvis_snapshot WHERE singleton = 1"
    ).fetchone()
    assert row == ("old",)


def test_read_new_format_facts_defaults_carried_from_sha_to_none(tmp_path):
    # A snapshot written BEFORE this feature has no scip_carried_from_sha
    # column; the read path must return None, not raise.
    conn = _facts_fixture(tmp_path)
    finalize_snapshot(conn, generation="g", commit_sha="new",
                      published_at="2026-10-18T00:00:00+00:00",
                      source_hash="s", scip_state="available")
    conn.execute("ALTER TABLE jarvis_snapshot DROP COLUMN scip_carried_from_sha")
    facts = _read_new_format_facts(conn)
    assert facts.scip_carried_from_sha is None
```

In `tests/test_server_tools.py` or wherever `read_metadata` is tested:

```python
def test_read_metadata_parses_scip_carried_from_sha(tmp_path):
    d = tmp_path / "scip" / "p" / "r" / "b"
    d.mkdir(parents=True)
    (d / "index-x.metadata.json").write_text(json.dumps({
        "project": "p", "repo": "r", "branch": "b",
        "commit_sha": "new", "scip_carried_from_sha": "oldsha",
    }))
    meta = read_metadata(str(tmp_path), "p", "r", "b", "index-x.db")
    assert meta.scip_carried_from_sha == "oldsha"
    assert meta.commit_sha == "new"
```

`_facts_fixture` follows `test_syntax_index.py`'s existing pattern
(`test_copy_syntax_tables_and_finalize_snapshot_derive_real_coverage`,
`tests/test_syntax_index.py:274`): a tiny git repo, `capture_sources` +
`build_syntax_index` into `syntax.db`, then `copy_syntax_tables` into a
fresh converter-shaped db and return that connection:

```python
def _facts_fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    manifest = capture_sources(repo, scratch)
    build_syntax_index(scratch / "syntax.db", manifest, pool=ParserPool())
    conn = sqlite3.connect(scratch / "index.db")
    copy_syntax_tables(scratch / "syntax.db", conn)
    return conn
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_registry.py tests/test_syntax_index.py tests/test_query.py -v -k carried`
Expected: FAIL — `ValueError: invalid scip_state 'carried'` / missing kwarg / missing attribute.

- [ ] **Step 3: Implement**

1. `registry.py:43`: add `"carried"` to the `SCIP_STATES` frozenset.
2. `syntax_index.py` DDL: add two nullable last columns to `jarvis_snapshot`:
   `scip_carried_from_sha TEXT` and `scip_carry_baseline INTEGER` (the
   stable escalation denominator — document count at the last FULL run,
   propagated across chained carries).
3. `SnapshotFacts`: add `scip_carried_from_sha: str | None = None` and
   `scip_carry_baseline: int | None = None`.
4. `finalize_snapshot`: new keyword params `scip_carried_from_sha: str | None = None`
   and `scip_carry_baseline: int | None = None`; extend the `INSERT OR REPLACE`
   column list and values tuple; pass both through to the returned `SnapshotFacts`.
5. `_read_new_format_facts`: read both new columns only when present —
```python
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jarvis_snapshot)")}
    carried_from = (
        conn.execute("SELECT scip_carried_from_sha FROM jarvis_snapshot").fetchone()[0]
        if "scip_carried_from_sha" in columns else None
    )
    carry_baseline = (
        conn.execute("SELECT scip_carry_baseline FROM jarvis_snapshot").fetchone()[0]
        if "scip_carry_baseline" in columns else None
    )
```
   (adjust to the function's existing row-unpacking style), and pass both to
   `SnapshotFacts`.
6. `index_reader.py`: add `scip_carried_from_sha: str | None = None` to `IndexMetadata`; in `read_metadata` add `scip_carried_from_sha=raw.get("scip_carried_from_sha")`.
7. Check the state's consumers: update the comment above `SCIP_STATES`
   (`registry.py:41`), and grep for enumerations of scip states in
   `registry.recovery_for` and `dashboard.py` — add a `carried` branch
   (recovery hint: "run an explicit `jarvis index` to refresh") wherever
   states are mapped to advice. `server.py` passes the string through
   untouched, so it needs no change.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry.py tests/test_syntax_index.py tests/test_query.py tests/test_server_tools.py -v`
Expected: PASS, including all pre-existing tests (old snapshots without the column read as None).

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/registry.py src/jarvis/syntax_index.py src/jarvis/index_reader.py tests/
git commit -m "feat(index): carried scip state, snapshot facts, metadata field"
```

---

### Task 4: Pipeline integration — the stage-3 three-way choice

**Files:**
- Modify: `src/jarvis/index_cli.py:1505-1509` (stage 3), `:1547-1604` (publish/graph), `:1612-1623` (metadata write), `:1627-1654` (terminal stage decision), `:1676-1698` (notes)
- Test: `tests/test_index_cli.py`

**Interfaces:**
- Consumes: `changed_paths`, `plan_carry`, `should_escalate`, `apply_carry`, `carried_from_sha`, `CarryPlan` from `jarvis.carry`; `index_has_navigation_data` (already in this module); `_open_previous_snapshot` (already in this module, `index_cli.py:1137`).
- Produces: watch runs publish generations with `scip_state="carried"` and metadata `scip_carried_from_sha`; registry rows record `carried`; everything else observable is unchanged.

- [ ] **Step 1: Write the failing test**

In `tests/test_index_cli.py` (following its existing fake-subprocess conventions; add `from uuid import uuid4` and `from jarvis import index_cli` to the imports, set `JARVIS_DATA_DIR` to `tmp_path` first — the module's house pattern):

```python
def test_watch_run_carries_scip_without_spawning_indexer(tmp_path, monkeypatch):
    repo = _offer_test_repo(tmp_path, "carry-repo")  # existing helper: copytree of fixtures/mini_py_repo + git init + commit
    slug = "carry-test"

    _snapshot_with_scip(tmp_path).close()   # builds tmp_path/"previous.db"
    # A FRESH connection per call: stage 2 opens AND closes its own
    # `_open_previous_snapshot` result, so returning one shared object
    # hands stage 3 a closed connection (review finding — the planning
    # except would silently escalate and the test would fail on its own
    # tripwire).
    monkeypatch.setattr(index_cli, "_open_previous_snapshot",
                        lambda *_: sqlite3.connect(tmp_path / "previous.db"))

    spawned = []
    def _fail_if_spawned(*a, **k):
        spawned.append(a)
        raise AssertionError("indexer must not spawn on a carry-eligible watch run")
    monkeypatch.setattr(index_cli, "_attempt_scip", _fail_if_spawned)
    monkeypatch.setattr(index_cli, "_run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""))  # zoekt step stub

    slug_out = index_cli.index_repo(repo, slug=slug, watch=True)

    assert spawned == []
    registry = Registry(config.data_dir() / "registry.db")
    try:
        assert registry.get(slug).scip_state == "carried"
    finally:
        registry.close()
    pointer = (config.index_dir(slug) / "current").read_text().strip()
    meta = json.loads((config.index_dir(slug) / pointer.removesuffix(".db")
                       + ".metadata.json").read_text())
    assert meta["scip_carried_from_sha"] == "oldsha"


def test_explicit_run_never_carries(tmp_path, monkeypatch):
    repo = _offer_test_repo(tmp_path, "carry-repo")
    _snapshot_with_scip(tmp_path).close()
    monkeypatch.setattr(index_cli, "_open_previous_snapshot",
                        lambda *_: sqlite3.connect(tmp_path / "previous.db"))
    calls = []
    monkeypatch.setattr(index_cli, "_attempt_scip",
                        lambda *a, **k: calls.append(a) or _available_attempt(tmp_path))
    monkeypatch.setattr(index_cli, "_run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""))

    index_cli.index_repo(repo, slug="carry-test", watch=False)

    assert len(calls) == 1  # full SCIP ran even though a carry-eligible snapshot existed
```

Concrete helpers for the same file:

```python
def _snapshot_with_scip(tmp_path, *, with_nav: bool = True) -> sqlite3.Connection:
    """A previous published snapshot: five SCIP tables + syntax_files +
    jarvis_snapshot(commit_sha='oldsha', scip_state='available'). When
    with_nav is False the mentions/chunks tables stay empty — a snapshot
    whose SCIP data is unusable."""
    conn = sqlite3.connect(tmp_path / "previous.db")
    conn.executescript(_SCIP_TABLES_DDL)          # from jarvis.carry (test re-declares it locally if import of a private name is unwanted)
    conn.executescript(
        "CREATE TABLE syntax_files (file_path TEXT PRIMARY KEY, language TEXT,"
        " file_hash TEXT, parser_identity TEXT, in_scope INTEGER, state TEXT,"
        " reason TEXT, scip_outline INTEGER DEFAULT 0, scip_definition INTEGER DEFAULT 0);"
        "CREATE TABLE jarvis_snapshot (singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
        " format_version INTEGER NOT NULL, generation TEXT NOT NULL, commit_sha TEXT NOT NULL,"
        " published_at TEXT NOT NULL, source_hash TEXT NOT NULL, scip_state TEXT NOT NULL,"
        " scip_outlines INTEGER NOT NULL, scip_definitions INTEGER NOT NULL,"
        " scip_references INTEGER NOT NULL, scip_calls INTEGER NOT NULL,"
        " scip_types INTEGER NOT NULL, syntax_counts TEXT NOT NULL,"
        " scip_carried_from_sha TEXT);")
    if with_nav:
        # Two documents so a one-file diff stays under the 80% escalation
        # threshold (1/2 = 50%). other.py is self-contained: it defines its
        # own symbol and never mentions greeter.py's, so the closure stops
        # at the changed document.
        conn.executemany(
            "INSERT INTO documents (id, relative_path) VALUES (?, ?)",
            [(1, "greeter.py"), (2, "other.py")])
        conn.executemany(
            "INSERT INTO chunks (id, document_id, chunk_index, start_line,"
            " end_line, occurrences) VALUES (?, ?, 0, 1, 2, ?)",
            [(1, 1, scip_encoder.encode_occurrences([])),
             (2, 2, scip_encoder.encode_occurrences([]))])
        conn.executemany(
            "INSERT INTO global_symbols (id, symbol) VALUES (?, ?)",
            [(9, "greeter py greet()."), (10, "other py helper().")])
        conn.executemany(
            "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?, ?, ?)",
            [(1, 9, 1), (2, 10, 1)])   # each doc defines exactly its own symbol
    # syntax_files lists ONLY greeter.py: mini_py_repo tracks exactly that
    # file, so the diff sees one changed path. other.py exists only as a
    # previous SCIP document (no syntax row) — `changed_paths` never flags
    # it, so the closure leaves its rows carried.
    conn.execute(
        "INSERT INTO syntax_files (file_path, file_hash, in_scope, state)"
        " VALUES ('greeter.py', 'stale-hash', 1, 'parsed')")
    conn.execute(
        "INSERT INTO jarvis_snapshot VALUES (1, 1, 'g-old', 'oldsha',"
        " '2026-10-17T00:00:00Z', 'sh-old', 'available', 1, 1, 1, 1, 1, '{}', NULL)")
    conn.commit()
    return conn


def _available_attempt(tmp_path) -> "index_cli.ScipAttempt":
    """Converter output with TWO documents (same 1-of-2 under-threshold
    arithmetic as `_snapshot_with_scip`) so the Task 6 e2e's follow-up
    watch run carries instead of escalating. A UNIQUE path per call: the
    pipeline merges syntax tables and the snapshot row into `db_path` in
    place, so a shared path would hand later runs a mutated db (review
    finding — UNIQUE violation on re-insert)."""
    db = tmp_path / f"converted-{uuid4().hex}.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCIP_TABLES_DDL)
    conn.executemany(
        "INSERT INTO documents (id, relative_path) VALUES (?, ?)",
        [(1, "greeter.py"), (2, "other.py")])
    conn.executemany(
        "INSERT INTO chunks (id, document_id, chunk_index, start_line,"
        " end_line, occurrences) VALUES (?, ?, 0, 1, 2, ?)",
        [(1, 1, scip_encoder.encode_occurrences([])),
         (2, 2, scip_encoder.encode_occurrences([]))])
    conn.executemany(
        "INSERT INTO global_symbols (id, symbol) VALUES (?, ?)",
        [(9, "greeter py greet()."), (10, "other py helper().")])
    conn.executemany(
        "INSERT INTO mentions (chunk_id, symbol_id, role) VALUES (?, ?, ?)",
        [(1, 9, 1), (2, 10, 1)])
    conn.commit()
    conn.close()
    return index_cli.ScipAttempt(state="available", db_path=db)
```

Third test — spec §10 item 5, escalation-not-corruption (previous SCIP unusable → full run):

```python
def test_watch_run_with_unusable_previous_scip_runs_full(tmp_path, monkeypatch):
    repo = _offer_test_repo(tmp_path, "carry-repo")
    _snapshot_with_scip(tmp_path, with_nav=False).close()
    monkeypatch.setattr(index_cli, "_open_previous_snapshot",
                        lambda *_: sqlite3.connect(tmp_path / "previous.db"))
    calls = []
    monkeypatch.setattr(index_cli, "_attempt_scip",
                        lambda *a, **k: calls.append(a) or _available_attempt(tmp_path))
    monkeypatch.setattr(index_cli, "_run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""))

    index_cli.index_repo(repo, slug="carry-test", watch=True)

    assert len(calls) == 1  # no usable previous data -> no carry, full attempt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_index_cli.py -v -k carry`
Expected: FAIL — `_attempt_scip` was called / `scip_state` is `available` not `carried`.

- [ ] **Step 3: Implement in `index_cli.py`**

1. Import at top: `from jarvis.carry import CarryPlan, apply_carry, carried_from_sha, changed_paths, plan_carry, should_escalate`.

2. Replace the stage-3 block (`# -- Stage 3: optional SCIP enrichment`):

```python
            # -- Stage 3: optional SCIP enrichment -----------------------
            attempt: ScipAttempt | None = None
            carried: CarryPlan | None = None
            carry_prev: sqlite3.Connection | None = None
            if attempt_scip and watch:
                prev = _open_previous_snapshot(slug, root)
                if prev is not None:
                    try:
                        if index_has_navigation_data(prev):
                            plan = plan_carry(prev, changed_paths(prev, manifest))
                            if not should_escalate(plan):
                                carried, carry_prev = plan, prev
                        if carry_prev is None:
                            prev.close()
                    except Exception:
                        # Design §8: any planning failure escalates to a
                        # full run — worst case equals today's behavior.
                        carried = None
                        prev.close()
            if attempt_scip and carried is None:
                attempt = _attempt_scip(repo_path, resolved_language, scheme,
                                        slug, scratch)
```

3. Publish block — add the carry branch beside the `attempt.db_path` branch:

```python
            carried_sha: str | None = None
            if attempt is not None and attempt.db_path is not None:
                # ... existing converter-db branch unchanged ...
            elif carried is not None and carry_prev is not None:
                # Design §6: merge the other way around — SCIP rows into
                # this run's syntax.db, primary keys preserved verbatim.
                carried_sha = carried_from_sha(carry_prev)
                final_db = syntax_db
                conn = sqlite3.connect(final_db)
                try:
                    apply_carry(conn, carry_prev, carried)
                    facts = finalize_snapshot(
                        conn, generation=generation, commit_sha=sha,
                        published_at=published_at,
                        source_hash=manifest.source_hash,
                        scip_state="carried",
                        scip_carried_from_sha=carried_sha,
                        scip_carry_baseline=carried.baseline_documents,
                    )
                    has_nav = index_has_navigation_data(conn)
                finally:
                    conn.close()
            else:
                # ... existing SCIP-less branches unchanged ...
```

4. Graph block (`:1593`): the condition becomes
```python
                if (attempt is not None and attempt.db_path is not None) or carried is not None:
```
   (carried generations populate graph edges from the merged tables; only truly SCIP-less generations clear them).

5. Metadata write (`:1612-1623`): after `"source_hash": manifest.source_hash,` add
```python
                    **({"scip_carried_from_sha": carried_sha}
                       if carried_sha is not None else {}),
```

6. Terminal stage decision (`:1632`): insert before the `elif attempt is not None:` arm:
```python
        elif carried is not None:
            stage = ScipStageFields(state="carried")
```

7. Close `carry_prev` in the run's cleanup (the `finally` that closes `previous` at `:1493-1495` is stage-2-scoped; add `if carry_prev is not None: carry_prev.close()` to the outer `finally` that closes `registry`).

8. User-facing note (next to the suppressed-run note at `:1676`):
```python
        if carried is not None:
            kept = carried.total_documents - len(carried.invalidated)
            print(
                f"note: {slug}: SCIP carried forward from "
                f"{(carried_sha or '?')[:12]} ({kept}/{carried.total_documents} "
                f"documents); refresh with: jarvis index {repo_path}",
                file=sys.stderr,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_index_cli.py -v`
Expected: PASS — new carry tests plus every existing pipeline test.

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/index_cli.py tests/test_index_cli.py
git commit -m "feat(index): watch runs carry scip forward via closure"
```

---

### Task 5: Freshness — `scip_as_of`

**Files:**
- Modify: `src/jarvis/query.py:64-103` (`FreshnessSnapshot`, `_freshness_snapshot`), `:810-841` (`get_index_status`)
- Test: `tests/test_query.py`

**Interfaces:**
- Consumes: `IndexMetadata.scip_carried_from_sha` (Task 3).
- Produces: `FreshnessSnapshot.scip_as_of: str | None = None`; staleness true when `scip_as_of` differs from the live HEAD. `server._freshness_fields` serializes it automatically via `asdict`.

- [ ] **Step 1: Write the failing test**

```python
def test_freshness_reports_scip_as_of_stale(tmp_path):
    # Snapshot at new HEAD whose SCIP data reflects oldsha
    meta = IndexMetadata(project="_", repo="r", branch="_", commit_sha="new",
                         published_at="2026-10-18T00:00:00+00:00",
                         generation="g", source_hash="s",
                         scip_carried_from_sha="old")
    snap = _freshness_snapshot(meta)
    assert snap.commit == "new"
    assert snap.scip_as_of == "old"


def test_get_index_status_stale_when_carried_sha_differs(tmp_path, monkeypatch):
    ...build published index + metadata with scip_carried_from_sha="old"...
    monkeypatch live HEAD to "new" (existing _git_head stubbing pattern)
    indexed, freshness = service.get_index_status("r", str(repo_path))
    assert indexed is True
    assert freshness.stale is True
    assert freshness.scip_as_of == "old"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_query.py -v -k "as_of or carried"`
Expected: FAIL — no `scip_as_of` attribute.

- [ ] **Step 3: Implement**

1. `FreshnessSnapshot`: add `scip_as_of: str | None = None` (keyword default, after `generation`).
2. `_freshness_snapshot`: pass `scip_as_of=metadata.scip_carried_from_sha` in the populated return (and `scip_as_of=None` in the empty-metadata return).
3. `get_index_status` staleness line (`:836`):
```python
        stale = snapshot.commit != live_head or (
            snapshot.scip_as_of is not None and snapshot.scip_as_of != live_head
        )
```
   and thread `scip_as_of=snapshot.scip_as_of` through the two reconstructed `FreshnessSnapshot` returns in that function (the `live_head is None` early return at `:827` and the final one at `:837`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_query.py tests/test_server_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/query.py tests/test_query.py
git commit -m "feat(server): freshness reports scip as-of sha for carried snapshots"
```

---

### Task 6: End-to-end watch flow + docs

**Files:**
- Test: `tests/test_index_cli.py`
- Modify: `docs/system-architecture.md` (stage-3 section), `CHANGELOG.md`

**Interfaces:**
- Consumes: everything above.
- Produces: one test proving the full watch flow; updated docs.

- [ ] **Step 1: Write the failing test**

```python
def test_watch_full_flow_small_diff_then_explicit_refresh(tmp_path, monkeypatch):
    """Acceptance criteria 1+2 (spec §12): a watch run over a small diff
    publishes carried without spawning the indexer; the explicit run after
    it spawns the indexer and publishes `available`."""
    repo = _offer_test_repo(tmp_path, "e2e-repo")
    # First run: explicit, full SCIP via a fake converter db
    monkeypatch.setattr(index_cli, "_attempt_scip", lambda *a, **k: _available_attempt(tmp_path))
    monkeypatch.setattr(index_cli, "_run", lambda *a, **k: subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""))  # zoekt step stub (Task 4 pattern)
    index_cli.index_repo(repo, slug="e2e-carry")
    # Small edit to one leaf file
    (repo / "greeter.py").write_text("def greet():\n    return 'hi v2'\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "edit"], check=True)

    spawned = []
    monkeypatch.setattr(index_cli, "_attempt_scip",
                        lambda *a, **k: spawned.append(1) or _available_attempt(tmp_path))
    index_cli.index_repo(repo, slug="e2e-carry", watch=True)
    assert spawned == []                       # indexer skipped
    registry = Registry(config.data_dir() / "registry.db")
    try:
        assert registry.get("e2e-carry").scip_state == "carried"
    finally:
        registry.close()

    index_cli.index_repo(repo, slug="e2e-carry", watch=False)
    assert len(spawned) == 1                   # explicit run refreshed
    registry = Registry(config.data_dir() / "registry.db")
    try:
        assert registry.get("e2e-carry").scip_state == "available"
    finally:
        registry.close()
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_index_cli.py -v -k watch_full_flow`
Expected: PASS after Task 4 (this test is the regression net for the whole feature; if it fails, the failure names the acceptance criterion that broke).

- [ ] **Step 3: Update docs**

- `docs/system-architecture.md`, stage-3 paragraph: append the carry-forward behavior, the 80% escalation, and the `carried` state.
- `CHANGELOG.md`: new unreleased entry — `jarvis watch now carries SCIP data forward for documents outside the edit's reverse-symbol closure; explicit index runs always run the full indexer.`

- [ ] **Step 4: Run the full unit gate**

Run: `uv run pytest -m "not integration" -rs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_index_cli.py docs/system-architecture.md CHANGELOG.md
git commit -m "feat(index): carry-forward watch flow end to end"
```
