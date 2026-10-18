"""SQLite-backed registry of indexed repos: path, slug, language, commit_sha,
run status, per-repo settings, and — since the tree-sitter syntax baseline
(spec TSI-06/TSI-08) — the reversible `scip_enabled` user choice plus the
persisted SCIP stage state, kept separate from the overall run-failure fields.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Transient marker written when a run starts; every terminal decision
# replaces it (spec TSI-06 vocabulary: failed > degraded > partial >
# indexed, plus this transient `indexing`).
INDEXING_STATUS = "indexing"

# Published with documented syntax/SCIP extraction gaps (TSI-06): a success
# variant — exit 0. Broadened by spec §12 from the old "zero SCIP chunks"
# meaning; the string is unchanged.
PARTIAL_STATUS = "partial"

# Baseline published but the enabled SCIP stage failed, is unavailable, or
# remains suppressed after a known failure at the same commit (TSI-06).
# Exit 0. Supersedes the Phase-3 opt-in FALL-01 degrade (spec §12).
DEGRADED_STATUS = "degraded"

# Origin taxonomy for failed rows (D-01): stored as readable slugs so they
# can be inspected straight from the sqlite3 CLI. `signature`/`manual`/
# `fallback` are historical values from the search-only design; the
# migration preserves them on legacy rows and `recovery_for` still derives
# their recovery strings, but no new row is ever written with them.
ORIGIN_FAILED_HARD = "failed_hard"
ORIGIN_SIGNATURE = "signature"
ORIGIN_MANUAL = "manual"
ORIGIN_FALLBACK = "fallback"

# Persisted SCIP stage vocabulary (spec TSI-06). `unknown` is a READ-time
# normalization for historical rows with insufficient evidence; writers use
# None (no attempt recorded) or one of the states below.
SCIP_STATES = frozenset(
    {"available", "partial", "failed", "unavailable", "unsupported", "disabled", "carried"}
)
SCIP_UNKNOWN_STATE = "unknown"

# Recorded as the language when a repo has no SCIP-indexable source at all.
# `registry.language` is NOT NULL, so this has to be a value rather than
# NULL. The syntax baseline still covers such a repo (spec TSI-07: no
# SCIP-supported language means `scip_state="unsupported"`, not a skipped
# baseline).
UNKNOWN_LANGUAGE = "unknown"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    slug TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    language TEXT NOT NULL,
    commit_sha TEXT,
    last_indexed TEXT NOT NULL,
    status TEXT NOT NULL,
    scheme_override TEXT,
    semantic_indexed_at TEXT,
    semantic_include TEXT,
    language_override TEXT,
    tracked_files INTEGER,
    status_origin TEXT,
    status_reason TEXT,
    status_stderr TEXT,
    scip_enabled INTEGER NOT NULL DEFAULT 1,
    scip_state TEXT,
    scip_failure_reason TEXT,
    scip_failure_stderr TEXT,
    scip_failed_at_sha TEXT,
    semantic_declined INTEGER
)
"""

# Note attached by the migration to legacy manual opt-out rows whose run
# never recorded a cause, so `jarvis status` explains why the row reads
# degraded instead of the row silently changing shape.
_LEGACY_OPT_OUT_NOTE = (
    "indexed by an earlier jarvis version without the syntax baseline; "
    "reindex to publish one"
)

_SELECT_COLUMNS = (
    "slug, path, language, commit_sha, last_indexed, status, scheme_override, "
    "semantic_indexed_at, semantic_include, language_override, tracked_files, "
    "status_origin, status_reason, status_stderr, "
    "scip_enabled, scip_state, scip_failure_reason, scip_failure_stderr, "
    "scip_failed_at_sha, scip_carried_from_sha, scip_carry_depth, "
    "semantic_declined"
)


def _ensure_column(conn: sqlite3.Connection, name: str, decl: str) -> None:
    """Idempotent `ALTER TABLE repos ADD COLUMN` for databases created before
    `name` existed.

    A "duplicate column name" `OperationalError` means a previous run (or a
    fresh `_SCHEMA` create) already added it, so it is ignored. Any other
    `OperationalError` -- notably "database is locked" from a concurrent
    `jarvis watch` reindex -- is re-raised rather than swallowed: a lock
    timeout during migration would otherwise look identical to "already
    exists" while actually leaving the column missing.
    """
    try:
        conn.execute(f"ALTER TABLE repos ADD COLUMN {name} {decl}")
        conn.commit()
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc):
            raise


def _column_names(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(repos)")}


def _migrate_legacy_choice_columns(conn: sqlite3.Connection) -> None:
    """One idempotent, transactional migration from the superseded
    `search_only`/`fallback_enabled` storage to `scip_enabled` + the SCIP
    stage fields (spec TSI-08, consolidating the per-column migration
    commits).

    Mapping (spec §9, brief case 10):

    - `search_only=1` with manual or NULL origin (the user asked for
      search-only) becomes `scip_enabled=0` — an intentional disable. The
      outcome remaps to `degraded`: the row must not claim a syntax
      snapshot exists, and until reindex there is no navigation pointer
      (`recovery_for`'s disabled branch supplies the reindex recovery).
    - Automatically classified fallback rows (`search_only=1` with the
      `signature` origin, or the Phase-3 `fallback` degraded rows) stay
      enabled: the known failure is copied into the stage fields
      (`scip_state='failed'` + reason/stderr/failed-at-sha) and the next
      explicit run retries enrichment (FALL-03, retained by TSI-11).

    Idempotent twice over: a database already carrying the new columns
    without the legacy ones skips the whole function, and the copy plus the
    column drops commit atomically, so a crash cannot leave a half-copied
    row or a half-dropped schema. Historical `status` values other than
    `search-only` are never rewritten, and no failure text is discarded.
    """
    names = _column_names(conn)
    if "search_only" not in names and "fallback_enabled" not in names:
        return

    # _ensure_column commits after every ALTER, so no transaction is open
    # here; BEGIN makes the copy + drops one all-or-nothing unit.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "search_only" in names:
            # Automatic (signature-classified) search-only rows: failure
            # evidence moves into the stage fields, enrichment stays enabled.
            conn.execute(
                "UPDATE repos SET "
                "scip_state = 'failed', "
                "scip_failure_reason = status_reason, "
                "scip_failure_stderr = status_stderr, "
                "scip_failed_at_sha = commit_sha, "
                "status = 'degraded' "
                "WHERE search_only = 1 "
                "AND COALESCE(status_origin, '') IN ('signature', 'fallback')"
            )
            # Manual or unknown-origin opt-outs: the reversible disable.
            conn.execute(
                "UPDATE repos SET "
                "scip_enabled = 0, "
                "scip_state = 'disabled', "
                "status = 'degraded', "
                "status_reason = COALESCE(NULLIF(status_reason, ''), ?) "
                "WHERE search_only = 1 "
                "AND COALESCE(status_origin, 'manual') NOT IN ('signature', 'fallback')",
                (_LEGACY_OPT_OUT_NOTE,),
            )
        if "fallback_enabled" in names:
            # Phase-3 degraded rows (fallback enabled, full build failed,
            # search republished): degraded remains degraded, enabled, with
            # the failed commit/cause copied into the stage fields.
            conn.execute(
                "UPDATE repos SET "
                "scip_state = 'failed', "
                "scip_failure_reason = COALESCE(NULLIF(scip_failure_reason, ''), status_reason), "
                "scip_failure_stderr = COALESCE(NULLIF(scip_failure_stderr, ''), status_stderr), "
                "scip_failed_at_sha = COALESCE(scip_failed_at_sha, commit_sha) "
                "WHERE status = 'degraded' AND scip_state IS NULL"
            )
        if "search_only" in names:
            conn.execute("ALTER TABLE repos DROP COLUMN search_only")
        if "fallback_enabled" in names:
            conn.execute("ALTER TABLE repos DROP COLUMN fallback_enabled")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _split_include(raw: str | None) -> tuple[str, ...]:
    """Force-include prefixes are stored newline-joined; NULL or empty
    means none. Newline is a safe separator — a path prefix cannot
    contain one."""
    return tuple(prefix for prefix in (raw or "").split("\n") if prefix)


def _join_include(prefixes: tuple[str, ...]) -> str | None:
    return "\n".join(prefixes) or None


@dataclass(frozen=True)
class ScipStageFields:
    """Stage-scoped SCIP fields for one terminal run decision (spec TSI-06).

    `Registry.upsert` writes these six columns if and only if a
    `ScipStageFields` is supplied, so a transitional `indexing` write (and
    any legacy-shaped upsert) can never erase a prior failure — the narrowed
    D-04 (spec §12): successful baseline bookkeeping cannot erase an active
    SCIP failure, and successful enrichment clears its own fields by passing
    an explicit record."""

    state: str
    failure_reason: str | None = None
    failure_stderr: str | None = None
    failed_at_sha: str | None = None
    carried_from_sha: str | None = None
    carry_depth: int = 0


@dataclass(frozen=True)
class RegisteredRepo:
    slug: str
    path: str
    language: str
    commit_sha: str | None
    last_indexed: datetime
    status: str  # "indexed" | "indexing" | "failed" | "partial" | "degraded"
    scheme_override: str | None = None
    semantic_indexed_at: datetime | None = None
    semantic_include: tuple[str, ...] = ()
    language_override: str | None = None
    tracked_files: int | None = None
    # Failure/degradation cause (D-01/D-02/D-03): origin slug, one-line
    # classified reason, and the complete failure text. NULL on every
    # successful run -- `upsert` clears all three (D-04).
    status_origin: str | None = None
    status_reason: str | None = None
    status_stderr: str | None = None
    # The reversible user choice (spec TSI-06): default true; only an
    # explicit --no-scip (or a migrated legacy opt-out) ever turns it off.
    scip_enabled: bool = True
    # Persisted SCIP stage state + failure evidence, kept separate from the
    # overall status_* fields above (spec TSI-06).
    scip_state: str | None = None
    scip_failure_reason: str | None = None
    scip_failure_stderr: str | None = None
    scip_failed_at_sha: str | None = None
    scip_carried_from_sha: str | None = None
    scip_carry_depth: int = 0
    # Semantic-install decline memory (SEMA-01): 1 = user declined the
    # offer; NULL = never asked. No tri-state — NULL reads False.
    semantic_declined: bool = False


def _normalize_scip_state(raw: str | None) -> str | None:
    """Vocabulary validation at READ time (spec TSI-06): a value outside
    the known states normalizes to `unknown` instead of leaking garbage —
    while the row's failure *text* fields pass through untouched, so no
    historical failure evidence is discarded."""
    if raw is None:
        return None
    return raw if raw in SCIP_STATES else SCIP_UNKNOWN_STATE


def _row_to_repo(row: tuple) -> RegisteredRepo:
    (slug, path, language, commit_sha, last_indexed, status,
     scheme_override, semantic_indexed_at, semantic_include, language_override,
     tracked_files, status_origin, status_reason, status_stderr,
     scip_enabled, scip_state, scip_failure_reason, scip_failure_stderr,
     scip_failed_at_sha, scip_carried_from_sha, scip_carry_depth,
     semantic_declined) = row
    return RegisteredRepo(
        slug=slug,
        path=path,
        language=language,
        commit_sha=commit_sha,
        last_indexed=datetime.fromisoformat(last_indexed),
        status=status,
        scheme_override=scheme_override,
        semantic_indexed_at=datetime.fromisoformat(semantic_indexed_at) if semantic_indexed_at is not None else None,
        semantic_include=_split_include(semantic_include),
        language_override=language_override,
        tracked_files=tracked_files,
        status_origin=status_origin,
        status_reason=status_reason,
        status_stderr=status_stderr,
        scip_enabled=bool(scip_enabled),
        scip_state=_normalize_scip_state(scip_state),
        scip_failure_reason=scip_failure_reason,
        scip_failure_stderr=scip_failure_stderr,
        scip_failed_at_sha=scip_failed_at_sha,
        scip_carried_from_sha=scip_carried_from_sha,
        scip_carry_depth=scip_carry_depth,
        semantic_declined=bool(semantic_declined) if semantic_declined is not None else False,
    )


def origin_of(entry: RegisteredRepo) -> str | None:
    """The row's stored origin slug, or None. Legacy search-only rows that
    were never stamped keep a NULL origin: manual vs signature is
    unrecoverable post-hoc, and `recovery_for`'s degraded branch already
    gives migrated rows the right recovery without inventing one."""
    return entry.status_origin


def recovery_for(entry: RegisteredRepo) -> str | None:
    """Derive the recovery command from the row at read time (D-09) -- a
    mapping in code, never persisted per-row, so wording changes need no
    data migration. Returns None when there is nothing to recover from
    (successful rows, rows without failure evidence, unknown future
    origins).

    New-vocabulary branches come first (spec TSI-06: recovery maps to the
    reversible CLI and stage states); the origin branches below them exist
    for legacy rows the migration preserved verbatim."""

    if entry.status == "failed":
        return f"jarvis index {entry.path}"  # D-12: the full original command
    if entry.status == DEGRADED_STATUS:
        if entry.scip_enabled:
            if origin_of(entry) == ORIGIN_FALLBACK:
                # Legacy Phase-3 wording, kept verbatim for migrated rows.
                return (
                    f"fix the indexer failure, then `jarvis reindex {entry.slug}` "
                    "(full build retries automatically)"
                )
            return (
                f"fix the SCIP failure, then `jarvis index {entry.path} --scip` "
                "to retry enrichment"
            )
        return (
            f"`jarvis reindex {entry.slug}` publishes the syntax baseline; "
            f"`jarvis index {entry.path} --scip` re-enables SCIP enrichment"
        )
    origin = origin_of(entry)
    if origin == ORIGIN_FAILED_HARD:
        return f"jarvis index {entry.path}"
    if origin == ORIGIN_SIGNATURE:
        return f"jarvis reindex {entry.slug}"  # D-11: generic; per-signature remedy prose stays out
    if origin == ORIGIN_FALLBACK:
        # Legacy wording (Phase 3 Area 3).
        return (
            f"fix the indexer failure, then `jarvis reindex {entry.slug}` "
            "(full build retries automatically)"
        )
    if origin == ORIGIN_MANUAL:
        return f"jarvis forget {entry.slug} && jarvis index {entry.path}"  # legacy D-10 escape
    return None


class Registry:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        # `jarvis watch`'s background reindex and a manual `jarvis
        # index`/`reindex` can legitimately race on this same file — a
        # busy_timeout makes SQLite retry for up to 5s instead of raising
        # "database is locked" on the first contended write.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        _ensure_column(self._conn, "scheme_override", "TEXT")
        _ensure_column(self._conn, "semantic_indexed_at", "TEXT")
        _ensure_column(self._conn, "semantic_include", "TEXT")
        _ensure_column(self._conn, "language_override", "TEXT")
        _ensure_column(self._conn, "tracked_files", "INTEGER")
        _ensure_column(self._conn, "status_origin", "TEXT")
        _ensure_column(self._conn, "status_reason", "TEXT")
        _ensure_column(self._conn, "status_stderr", "TEXT")
        _ensure_column(self._conn, "scip_enabled", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(self._conn, "scip_state", "TEXT")
        _ensure_column(self._conn, "scip_failure_reason", "TEXT")
        _ensure_column(self._conn, "scip_failure_stderr", "TEXT")
        _ensure_column(self._conn, "scip_failed_at_sha", "TEXT")
        _ensure_column(self._conn, "scip_carried_from_sha", "TEXT")
        _ensure_column(self._conn, "scip_carry_depth", "INTEGER NOT NULL DEFAULT 0")
        # SEMA-01 decline memory. NULL = never answered (offer again);
        # written only via set_semantic_declined — never by upsert, whose
        # transitional `indexing` writes would NULL-reset it (the
        # tracked_files trap).
        _ensure_column(self._conn, "semantic_declined", "INTEGER")
        _migrate_legacy_choice_columns(self._conn)

    def upsert(
        self,
        slug: str,
        path: str,
        language: str,
        commit_sha: str | None,
        status: str,
        scheme_override: str | None = None,
        semantic_include: tuple[str, ...] = (),
        language_override: str | None = None,
        status_origin: str | None = None,
        status_reason: str | None = None,
        status_stderr: str | None = None,
        *,
        scip_enabled: bool | None = None,
        scip_stage: ScipStageFields | None = None,
    ) -> RegisteredRepo:
        """Insert or update the row for `slug`.

        Field groups (spec TSI-06):

        - `scip_enabled` is written on every upsert: the resolved choice
          for this run (explicit CLI flag, persisted choice, or the
          default). No search-only state exists anymore — the legacy
          column was dropped by the TSI-08 migration.
        - `scip_stage` is the terminal stage decision. When None (the
          transitional `indexing` write, or legacy-shaped upserts) the four
          stage columns are left untouched, so a transition to `indexing`
          preserves prior failure fields (spec TSI-06) exactly like
          `tracked_files`.
        - `tracked_files`, `semantic_indexed_at`, and `semantic_declined`
          are deliberately never touched here; each has its own writer.
        """
        if scip_stage is not None and scip_stage.state not in SCIP_STATES:
            raise ValueError(
                f"invalid scip_state {scip_stage.state!r} (expected one of {sorted(SCIP_STATES)})"
            )
        resolved_scip_enabled = True if scip_enabled is None else scip_enabled
        last_indexed = datetime.now(UTC)
        conflict_success_fields = (
            # D-04: success paths leave status_origin/status_reason/stderr at
            # their NULL defaults -- so excluded.* is NULL here, and listing
            # all three columns is what clears a stale failure on the next
            # successful index. The documented inverse of tracked_files',
            # semantic_declined's, and (when scip_stage is None) the stage
            # columns' deliberate exclusion.
            "status_origin=excluded.status_origin, "
            "status_reason=excluded.status_reason, "
            "status_stderr=excluded.status_stderr, "
        )
        base_values: tuple = (
            slug, path, language, commit_sha, last_indexed.isoformat(), status,
            scheme_override, _join_include(semantic_include), language_override,
            status_origin, status_reason, status_stderr,
        )
        if scip_stage is None:
            # Transitional `indexing` write (or a legacy-shaped upsert): the
            # stage columns are absent from the conflict list, so a prior
            # failure record survives untouched.
            sql = (
                "INSERT INTO repos (slug, path, language, commit_sha, last_indexed, status, "
                "scheme_override, semantic_indexed_at, semantic_include, language_override, "
                "status_origin, status_reason, status_stderr, scip_enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET "
                "path=excluded.path, language=excluded.language, commit_sha=excluded.commit_sha, "
                "last_indexed=excluded.last_indexed, status=excluded.status, "
                "scheme_override=excluded.scheme_override, "
                "semantic_include=excluded.semantic_include, "
                "language_override=excluded.language_override, "
                + conflict_success_fields
                + "scip_enabled=excluded.scip_enabled"
            )
            values = base_values + (int(resolved_scip_enabled),)
        else:
            sql = (
                "INSERT INTO repos (slug, path, language, commit_sha, last_indexed, status, "
                "scheme_override, semantic_indexed_at, semantic_include, language_override, "
                "status_origin, status_reason, status_stderr, "
                "scip_state, scip_failure_reason, scip_failure_stderr, scip_failed_at_sha, "
                "scip_carried_from_sha, scip_carry_depth, scip_enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET "
                "path=excluded.path, language=excluded.language, commit_sha=excluded.commit_sha, "
                "last_indexed=excluded.last_indexed, status=excluded.status, "
                "scheme_override=excluded.scheme_override, "
                "semantic_include=excluded.semantic_include, "
                "language_override=excluded.language_override, "
                + conflict_success_fields
                + "scip_state=excluded.scip_state, "
                "scip_failure_reason=excluded.scip_failure_reason, "
                "scip_failure_stderr=excluded.scip_failure_stderr, "
                "scip_failed_at_sha=excluded.scip_failed_at_sha, "
                "scip_carried_from_sha=excluded.scip_carried_from_sha, "
                "scip_carry_depth=excluded.scip_carry_depth, "
                "scip_enabled=excluded.scip_enabled"
            )
            values = base_values + (
                scip_stage.state, scip_stage.failure_reason,
                scip_stage.failure_stderr, scip_stage.failed_at_sha,
                scip_stage.carried_from_sha, scip_stage.carry_depth,
                int(resolved_scip_enabled),
            )
        self._conn.execute(sql, values)
        self._conn.commit()
        return RegisteredRepo(
            slug=slug, path=path, language=language, commit_sha=commit_sha,
            last_indexed=last_indexed, status=status, scheme_override=scheme_override,
            semantic_indexed_at=None, semantic_include=semantic_include,
            language_override=language_override,
            status_origin=status_origin, status_reason=status_reason,
            status_stderr=status_stderr,
            scip_enabled=resolved_scip_enabled,
            scip_state=scip_stage.state if scip_stage is not None else None,
            scip_failure_reason=scip_stage.failure_reason if scip_stage is not None else None,
            scip_failure_stderr=scip_stage.failure_stderr if scip_stage is not None else None,
            scip_failed_at_sha=scip_stage.failed_at_sha if scip_stage is not None else None,
            scip_carried_from_sha=(
                scip_stage.carried_from_sha if scip_stage is not None else None
            ),
            scip_carry_depth=scip_stage.carry_depth if scip_stage is not None else 0,
        )

    def mark_status(self, slug: str, status: str) -> None:
        self._conn.execute(
            "UPDATE repos SET status = ?, last_indexed = ? WHERE slug = ?",
            (status, datetime.now(UTC).isoformat(), slug),
        )
        self._conn.commit()

    def record_failure(
        self,
        slug: str,
        path: str,
        language: str,
        origin: str,
        reason: str,
        stderr: str,
    ) -> RegisteredRepo:
        """Persist a failed run's cause (D-05/D-06) and return the row.

        INSERT ... ON CONFLICT, not `mark_status`: a bare UPDATE silently
        no-ops when the failure preceded the first `upsert` -- exactly the
        hard-failed-first-index case this exists to record. The conflict
        branch deliberately does NOT touch the overrides,
        `semantic_indexed_at`, `tracked_files`, the SCIP stage fields, or
        `scip_enabled`: those are facts about the user's choice and the
        last recorded stage state, not about this hard failure (narrowed
        D-04, spec TSI-06) — a hard failure after a degraded stage must
        keep the stage evidence for the watch suppression predicate.
        """
        last_indexed = datetime.now(UTC)
        self._conn.execute(
            "INSERT INTO repos (slug, path, language, commit_sha, last_indexed, status, "
            "status_origin, status_reason, status_stderr) "
            "VALUES (?, ?, ?, NULL, ?, 'failed', ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET "
            "path=excluded.path, language=excluded.language, commit_sha=NULL, "
            "last_indexed=excluded.last_indexed, status='failed', "
            "status_origin=excluded.status_origin, "
            "status_reason=excluded.status_reason, "
            "status_stderr=excluded.status_stderr",
            (slug, path, language, last_indexed.isoformat(), origin, reason, stderr),
        )
        self._conn.commit()
        entry = self.get(slug)
        assert entry is not None  # the INSERT above guarantees the row exists
        return entry

    def mark_semantic_indexed(self, slug: str) -> None:
        self._conn.execute(
            "UPDATE repos SET semantic_indexed_at = ? WHERE slug = ?",
            (datetime.now(UTC).isoformat(), slug),
        )
        self._conn.commit()

    def get(self, slug: str) -> RegisteredRepo | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM repos WHERE slug = ?",
            (slug,),
        ).fetchone()
        return _row_to_repo(row) if row is not None else None

    def list(self) -> list[RegisteredRepo]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM repos ORDER BY slug"
        ).fetchall()
        return [_row_to_repo(row) for row in rows]

    def mark_tracked_files(self, slug: str, count: int) -> None:
        """Record how many git-tracked blobs the last successful index saw.

        Deliberately not a column on `upsert`: a reindex upserts `indexing`
        before the count is known, and `upsert`'s ON CONFLICT list would then
        reset it to NULL.
        """
        self._conn.execute(
            "UPDATE repos SET tracked_files = ? WHERE slug = ?", (count, slug)
        )
        self._conn.commit()

    def set_scip_enabled(self, slug: str, value: bool) -> None:
        """Persist the reversible `--scip`/`--no-scip` user choice (spec
        TSI-06/TSI-07).

        Only the CLI path needs this standalone writer (an explicit flag on
        a run that fails before its first upsert); the pipeline's own
        upserts carry the resolved value. Supersedes the removed
        `set_fallback_enabled` — the enablement is reversible, so no
        forget/reindex escape is required to re-enable SCIP (spec §12)."""
        self._conn.execute(
            "UPDATE repos SET scip_enabled = ? WHERE slug = ?", (int(value), slug)
        )
        self._conn.commit()

    def set_semantic_declined(self, slug: str, value: bool) -> None:
        """Persist the SEMA-01 per-repo decline memory (the y/N offer's
        "no" answer, or EOF/Ctrl-C at the prompt).

        Deliberately not an upsert column, exactly like tracked_files:
        `upsert`'s ON CONFLICT list would NULL-reset the memory on every
        transitional `indexing` write. Only 1 is ever written; clearing is
        row death via `jarvis forget` (once the extra exists anywhere the
        gate order makes the bit moot).
        """
        self._conn.execute(
            "UPDATE repos SET semantic_declined = ? WHERE slug = ?",
            (int(value), slug),
        )
        self._conn.commit()

    def forget(self, slug: str) -> bool:
        cursor = self._conn.execute("DELETE FROM repos WHERE slug = ?", (slug,))
        self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._conn.close()
