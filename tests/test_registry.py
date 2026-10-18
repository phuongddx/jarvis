from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

from jarvis.registry import Registry


def test_upsert_then_get_roundtrips(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    repo = reg.get("my-repo")
    assert repo is not None
    assert repo.path == "/repos/my-repo"
    assert repo.language == "python"
    assert repo.commit_sha == "abc123"
    assert repo.status == "indexed"
    # A plain success upsert carries no failure cause (D-04).
    assert repo.status_origin is None
    assert repo.status_reason is None
    assert repo.status_stderr is None
    reg.close()


def test_upsert_same_slug_updates_in_place(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    reg.upsert("my-repo", "/repos/my-repo", "python", "def456", "indexed")
    assert len(reg.list()) == 1
    assert reg.get("my-repo").commit_sha == "def456"
    reg.close()


def test_mark_status_transition(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("my-repo", "/repos/my-repo", "python", None, "indexing")
    reg.mark_status("my-repo", "failed")
    assert reg.get("my-repo").status == "failed"
    reg.close()


def test_get_unknown_slug_returns_none(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    assert reg.get("nope") is None
    reg.close()


def test_list_orders_by_slug(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("zeta", "/z", "python", None, "indexed")
    reg.upsert("alpha", "/a", "python", None, "indexed")
    assert [r.slug for r in reg.list()] == ["alpha", "zeta"]
    reg.close()


def test_forget_removes_row_and_reports_whether_it_existed(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("my-repo", "/repos/my-repo", "python", None, "indexed")
    assert reg.forget("my-repo") is True
    assert reg.get("my-repo") is None
    assert reg.forget("my-repo") is False
    reg.close()


def test_registry_persists_across_reopen(tmp_path: Path):
    db_path = tmp_path / "registry.db"
    Registry(db_path).upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    reopened = Registry(db_path)
    assert reopened.get("my-repo").commit_sha == "abc123"
    reopened.close()


def test_upsert_persists_scheme_override(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("my-repo", "/repos/my-repo", "swift", "abc123", "indexed", scheme_override="ios_theme_ui")
    repo = reg.get("my-repo")
    assert repo is not None
    assert repo.scheme_override == "ios_theme_ui"
    reg.close()


def test_upsert_defaults_scheme_override_to_none(tmp_path: Path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    assert reg.get("my-repo").scheme_override is None
    reg.close()


def test_scheme_override_survives_reopen_of_pre_existing_db(tmp_path: Path):
    """A registry.db written before this column existed must still open
    cleanly — the guarded ALTER TABLE has to be idempotent and safe against
    a database that predates the column."""
    db_path = tmp_path / "registry.db"
    Registry(db_path).upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    reopened = Registry(db_path)
    assert reopened.get("my-repo").scheme_override is None
    reopened.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed", scheme_override="foo")
    assert reopened.get("my-repo").scheme_override == "foo"
    reopened.close()




def test_mark_semantic_indexed_sets_timestamp(tmp_path: Path):
    registry = Registry(tmp_path / "registry.db")
    registry.upsert("r", "/p", "python", "sha", "indexed")
    assert registry.get("r").semantic_indexed_at is None
    registry.mark_semantic_indexed("r")
    assert registry.get("r").semantic_indexed_at is not None
    registry.close()


def test_upsert_preserves_semantic_timestamp(tmp_path: Path):
    registry = Registry(tmp_path / "registry.db")
    registry.upsert("r", "/p", "python", "sha", "indexed")
    registry.mark_semantic_indexed("r")
    stamp = registry.get("r").semantic_indexed_at
    registry.upsert("r", "/p", "python", "sha2", "indexed")
    assert registry.get("r").semantic_indexed_at == stamp
    registry.close()


def test_migration_is_idempotent(tmp_path: Path):
    Registry(tmp_path / "registry.db").close()
    Registry(tmp_path / "registry.db").close()  # second open must not raise


def test_semantic_include_roundtrips_as_tuple(tmp_path):
    from jarvis.registry import Registry
    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("r", "/p", "python", None, "indexed",
                        semantic_include=("src/gen", "vendor/pb"))
        assert registry.get("r").semantic_include == ("src/gen", "vendor/pb")
        assert registry.list()[0].semantic_include == ("src/gen", "vendor/pb")
    finally:
        registry.close()


def test_semantic_include_defaults_to_empty_tuple(tmp_path):
    from jarvis.registry import Registry
    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("r", "/p", "python", None, "indexed")
        assert registry.get("r").semantic_include == ()
    finally:
        registry.close()


def test_semantic_include_column_added_to_preexisting_db(tmp_path):
    """A registry.db created before this column exists must migrate in
    place rather than crash."""
    import sqlite3
    from jarvis.registry import Registry
    db = tmp_path / "registry.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE repos (slug TEXT PRIMARY KEY, path TEXT NOT NULL, "
        "language TEXT NOT NULL, commit_sha TEXT, last_indexed TEXT NOT NULL, "
        "status TEXT NOT NULL, scheme_override TEXT, semantic_indexed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('old', '/p', 'python', NULL, "
        "'2026-01-01T00:00:00+00:00', 'indexed', NULL, NULL)"
    )
    conn.commit()
    conn.close()

    registry = Registry(db)
    try:
        assert registry.get("old").semantic_include == ()
        registry.upsert("old", "/p", "python", None, "indexed",
                        semantic_include=("src/gen",))
        assert registry.get("old").semantic_include == ("src/gen",)
    finally:
        registry.close()


def test_upsert_persists_language_override(tmp_path: Path):
    registry = Registry(tmp_path / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed",
                    language_override="python")
    assert registry.get("my-repo").language_override == "python"
    registry.close()


def test_upsert_defaults_language_override_to_none(tmp_path: Path):
    registry = Registry(tmp_path / "registry.db")
    registry.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    assert registry.get("my-repo").language_override is None
    registry.close()


def test_language_override_column_added_to_preexisting_db(tmp_path: Path):
    """A registry.db written before this column existed must still open
    cleanly -- the guarded ALTER TABLE has to be idempotent and safe
    against a database that predates the column."""
    db_path = tmp_path / "registry.db"
    Registry(db_path).upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed")
    reopened = Registry(db_path)
    assert reopened.get("my-repo").language_override is None
    reopened.upsert("my-repo", "/repos/my-repo", "python", "abc123", "indexed",
                    language_override="java")
    assert reopened.get("my-repo").language_override == "java"
    reopened.close()




def test_upsert_round_trips_scip_enabled(tmp_path):
    """The reversible user choice (spec TSI-06): default true; an
    explicit scip_enabled on the upsert is the only writer besides the
    `set_scip_enabled` setter."""
    from jarvis.registry import Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("r", "/p", "java", None, "indexed")
        entry = registry.get("r")
        assert entry is not None
        assert entry.scip_enabled is True

        registry.upsert("r", "/p", "java", None, "indexed", scip_enabled=False)
        entry = registry.get("r")
        assert entry is not None
        assert entry.scip_enabled is False

        registry.upsert("r", "/p", "java", None, "indexed", scip_enabled=True)
        entry = registry.get("r")
        assert entry is not None
        assert entry.scip_enabled is True
    finally:
        registry.close()


def test_new_columns_migrate_onto_a_preexisting_database(tmp_path):
    """A registry created before the SCIP stage columns existed gains them
    additively on open; the legacy row keeps its facts and reads enabled
    with no recorded stage state (TSI-08: preserve repo paths, schemes,
    language choices, failure evidence)."""
    import sqlite3

    from jarvis.registry import Registry

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE repos (slug TEXT PRIMARY KEY, path TEXT NOT NULL, "
        "language TEXT NOT NULL, commit_sha TEXT, last_indexed TEXT NOT NULL, "
        "status TEXT NOT NULL, scheme_override TEXT, semantic_indexed_at TEXT, "
        "semantic_include TEXT, language_override TEXT)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('old', '/p', 'python', NULL, "
        "'2026-01-01T00:00:00+00:00', 'indexed', NULL, NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    registry = Registry(db)
    try:
        entry = registry.get("old")
        assert entry is not None
        assert entry.scip_enabled is True
        assert entry.scip_state is None
        assert entry.status == "indexed"
    finally:
        registry.close()


def test_stage_fields_survive_transitional_writes_but_terminal_ones_decide(tmp_path):
    """The stage-scoped field groups (spec TSI-06): a transitional
    `indexing` upsert must never erase a prior failure record; the
    terminal write replaces it wholesale; success clears it."""
    from jarvis.registry import Registry, ScipStageFields

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("r", "/p", "python", "s1", "degraded", scip_enabled=True,
                        scip_stage=ScipStageFields(
                            state="failed", failure_reason="build broke",
                            failure_stderr="full text", failed_at_sha="s1"))
        # Transitional write preserves everything.
        registry.upsert("r", "/p", "python", None, "indexing", scip_enabled=True)
        entry = registry.get("r")
        assert entry.scip_state == "failed"
        assert entry.scip_failure_reason == "build broke"
        assert entry.scip_failure_stderr == "full text"
        assert entry.scip_failed_at_sha == "s1"
        # A different terminal stage replaces the record wholesale.
        registry.upsert("r", "/p", "python", "s2", "indexed", scip_enabled=True,
                        scip_stage=ScipStageFields(state="available"))
        entry = registry.get("r")
        assert entry.scip_state == "available"
        assert entry.scip_failure_reason is None
        assert entry.scip_failure_stderr is None
        assert entry.scip_failed_at_sha is None
    finally:
        registry.close()


def test_scip_state_vocabulary_is_validated_on_write_and_normalized_on_read(tmp_path):
    """Writers reject an unknown stage state (spec TSI-06); a historical
    row carrying an unknown value normalizes to `unknown` at read time
    without touching its failure text."""
    import pytest
    from jarvis.registry import Registry, ScipStageFields

    registry = Registry(tmp_path / "registry.db")
    try:
        with pytest.raises(ValueError, match="invalid scip_state"):
            registry.upsert("r", "/p", "python", None, "indexed",
                            scip_stage=ScipStageFields(state="bogus"))
        registry.upsert("r", "/p", "python", None, "degraded",
                        scip_stage=ScipStageFields(state="failed",
                                                   failure_reason="kept"))
        registry._conn.execute("UPDATE repos SET scip_state = 'curious' WHERE slug = 'r'")
        registry._conn.commit()
        entry = registry.get("r")
        assert entry.scip_state == "unknown"
        assert entry.scip_failure_reason == "kept"
    finally:
        registry.close()




def test_ensure_column_is_idempotent(tmp_path: Path):
    """Second call must not raise: "duplicate column name" means a previous
    run (or a fresh _SCHEMA create) already added it."""
    import sqlite3

    from jarvis.registry import _ensure_column

    conn = sqlite3.connect(tmp_path / "r.db")
    conn.execute("CREATE TABLE repos (slug TEXT PRIMARY KEY)")
    _ensure_column(conn, "extra_col", "TEXT")
    _ensure_column(conn, "extra_col", "TEXT")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(repos)")}
    conn.close()
    assert "extra_col" in cols


def test_ensure_column_reraises_non_duplicate_errors(tmp_path: Path):
    """A lock timeout must not be swallowed as "already exists" -- that
    would leave the column missing while looking like success."""
    import sqlite3

    import pytest

    from jarvis.registry import _ensure_column

    conn = sqlite3.connect(tmp_path / "r.db")
    with pytest.raises(sqlite3.OperationalError):
        _ensure_column(conn, "c", "TEXT")  # no `repos` table exists
    conn.close()


def test_tracked_files_defaults_to_none_and_round_trips(tmp_path: Path):
    from jarvis.registry import Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
        assert registry.get("myslug").tracked_files is None
        registry.mark_tracked_files("myslug", 133)
        assert registry.get("myslug").tracked_files == 133
    finally:
        registry.close()


def test_mark_tracked_files_survives_a_later_upsert(tmp_path: Path):
    """upsert's ON CONFLICT list must not clobber tracked_files -- a reindex
    upserts status before the new count is known."""
    from jarvis.registry import Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
        registry.mark_tracked_files("myslug", 133)
        registry.upsert("myslug", "/repos/mine", "python", "def", "indexing")
        assert registry.get("myslug").tracked_files == 133
    finally:
        registry.close()


def _entry(**overrides):
    """A RegisteredRepo for recovery-mapping tests; keyword overrides pick
    the origin/state under test."""
    from datetime import UTC, datetime

    from jarvis.registry import RegisteredRepo

    fields = dict(
        slug="mine", path="/repos/mine", language="python", commit_sha=None,
        last_indexed=datetime.now(UTC), status="failed",
    )
    fields.update(overrides)
    return RegisteredRepo(**fields)


def test_record_failure_creates_row_when_absent(tmp_path: Path):
    """D-05: a hard-failed FIRST index must still leave a row via
    record_failure's INSERT..ON CONFLICT -- nothing else can explain the
    failure or make `jarvis reindex` resolve the slug."""
    from jarvis.registry import ORIGIN_FAILED_HARD, Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        returned = registry.record_failure(
            "mine", "/repos/mine", "python", ORIGIN_FAILED_HARD,
            "zoekt-git-index failed", "zoekt-git-index failed:\nboom",
        )
        assert returned.status == "failed"
        entry = registry.get("mine")
        assert entry is not None
        assert entry.status == "failed"
        assert entry.status_origin == ORIGIN_FAILED_HARD
        assert entry.status_reason == "zoekt-git-index failed"
        assert entry.status_stderr == "zoekt-git-index failed:\nboom"
        assert entry.commit_sha is None
    finally:
        registry.close()


def test_record_failure_overwrites_existing_row_and_preserves_choices(tmp_path: Path):
    """D-06: a failed reindex fully overwrites the run facts (commit_sha
    NULL, status failed, fresh last_indexed, failure fields stamped) while
    the user's SCIP choice and the recorded stage state survive -- losing
    the stage fields would break the watch suppression predicate's
    four-condition consult (spec TSI-06/TSI-07)."""
    import time
    from datetime import UTC, datetime

    from jarvis.registry import ORIGIN_FAILED_HARD, Registry, ScipStageFields

    registry = Registry(tmp_path / "registry.db")
    try:
        before = registry.upsert("mine", "/repos/mine", "python", "abc123",
                                 "degraded", scip_enabled=True,
                                 scip_stage=ScipStageFields(
                                     state="failed", failure_reason="build broke",
                                     failed_at_sha="abc123"))
        time.sleep(0.002)  # guarantee a strictly later last_indexed timestamp
        registry.record_failure(
            "mine", "/repos/mine", "python", ORIGIN_FAILED_HARD,
            "zoekt-git-index failed", "zoekt-git-index failed:\nboom",
        )
        entry = registry.get("mine")
        assert entry is not None
        assert entry.status == "failed"
        assert entry.commit_sha is None
        assert entry.last_indexed > before.last_indexed
        assert entry.status_origin == ORIGIN_FAILED_HARD
        assert entry.status_reason == "zoekt-git-index failed"
        assert entry.status_stderr == "zoekt-git-index failed:\nboom"
        # Stage fields and the user choice survive the hard failure.
        assert entry.scip_enabled is True
        assert entry.scip_state == "failed"
        assert entry.scip_failure_reason == "build broke"
        assert entry.scip_failed_at_sha == "abc123"
    finally:
        registry.close()


def test_recovery_for_derives_per_origin_commands():
    """D-09: recovery is derived at read time, never persisted. The new
    status branches come first (spec TSI-06); the origin branches exist
    for legacy rows the migration preserved verbatim."""
    from jarvis.registry import (
        ORIGIN_MANUAL,
        ORIGIN_SIGNATURE,
        recovery_for,
    )

    assert recovery_for(_entry(status="failed")) == "jarvis index /repos/mine"
    assert recovery_for(_entry(status="failed", status_origin="failed_hard")) == (
        "jarvis index /repos/mine"
    )
    assert recovery_for(_entry(status="search-only", status_origin=ORIGIN_SIGNATURE)) == (
        "jarvis reindex mine"
    )
    assert recovery_for(_entry(status="search-only", status_origin=ORIGIN_MANUAL)) == (
        "jarvis forget mine && jarvis index /repos/mine"
    )


def test_recovery_for_returns_none_for_successful_and_unknown_rows():
    from jarvis.registry import recovery_for

    assert recovery_for(_entry(status="indexed", commit_sha="abc")) is None
    assert recovery_for(_entry(status="search-only", status_origin="from-the-future")) is None


def test_recovery_for_degraded_rows_map_to_the_reversible_cli():
    """Spec TSI-06: recovery maps to the reversible CLI and stage states —
    an enabled degraded row names the SCIP retry; a disabled one names the
    baseline reindex first and the re-enable second; the legacy fallback
    wording survives verbatim for migrated rows."""
    from jarvis.registry import ORIGIN_FALLBACK, recovery_for

    enabled = recovery_for(_entry(status="degraded", scip_enabled=True))
    assert enabled == (
        "fix the SCIP failure, then `jarvis index /repos/mine --scip` to retry enrichment"
    )
    disabled = recovery_for(_entry(status="degraded", scip_enabled=False))
    assert "`jarvis reindex mine`" in disabled
    assert "--scip" in disabled
    legacy = recovery_for(_entry(status="degraded", scip_enabled=True,
                                 status_origin=ORIGIN_FALLBACK))
    assert "jarvis reindex mine" in legacy


def test_upsert_clears_failure_fields_on_success(tmp_path: Path):
    """D-04: failure fields are NULLed by the next successful index -- the
    registry must reflect the latest run, not lie about a stale failure
    (fail -> succeed -> read)."""
    from jarvis.registry import ORIGIN_FAILED_HARD, Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.record_failure(
            "mine", "/repos/mine", "python", ORIGIN_FAILED_HARD, "boom", "boom\ntrace"
        )
        registry.upsert("mine", "/repos/mine", "python", "abc123", "indexed")
        entry = registry.get("mine")
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.commit_sha == "abc123"
        assert entry.status_origin is None
        assert entry.status_reason is None
        assert entry.status_stderr is None
    finally:
        registry.close()


def test_failure_columns_migrate_onto_an_existing_database(tmp_path: Path):
    """SC5: a registry created before the failure columns existed opens
    via Registry, gains them additively, keeps its rows, and maps the
    legacy manual search-only row per TSI-08: intentional disable
    (`scip_enabled=0` + `disabled`), remapped to `degraded` with the
    migration note, its facts otherwise untouched."""
    from jarvis.registry import Registry

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE repos (slug TEXT PRIMARY KEY, path TEXT NOT NULL, "
        "language TEXT NOT NULL, commit_sha TEXT, last_indexed TEXT NOT NULL, "
        "status TEXT NOT NULL, scheme_override TEXT, semantic_indexed_at TEXT, "
        "semantic_include TEXT, language_override TEXT, "
        "search_only INTEGER NOT NULL DEFAULT 0, tracked_files INTEGER)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('indexed-old', '/p', 'python', 'abc', "
        "'2026-01-01T00:00:00+00:00', 'indexed', NULL, NULL, NULL, NULL, 0, 42)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('search-only-old', '/q', 'unknown', NULL, "
        "'2026-01-02T00:00:00+00:00', 'search-only', NULL, NULL, NULL, NULL, 1, 7)"
    )
    conn.commit()
    conn.close()

    registry = Registry(db)
    try:
        indexed = registry.get("indexed-old")
        assert indexed is not None
        assert indexed.status == "indexed"
        assert indexed.commit_sha == "abc"
        assert indexed.tracked_files == 42
        assert indexed.status_origin is None
        assert indexed.status_reason is None
        assert indexed.status_stderr is None
        assert indexed.scip_enabled is True

        legacy = registry.get("search-only-old")
        assert legacy is not None
        assert legacy.status == "degraded"  # remapped out of the dead vocabulary
        assert legacy.scip_enabled is False
        assert legacy.scip_state == "disabled"
        assert "reindex" in (legacy.status_reason or "")
        assert legacy.tracked_files == 7  # untouched by the migration
        assert legacy.status_origin is None
    finally:
        registry.close()

    probe = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in probe.execute("PRAGMA table_info(repos)")}
        assert {"status_origin", "status_reason", "status_stderr"} <= cols
        assert "search_only" not in cols
    finally:
        probe.close()


def test_upsert_round_trips_origin_parameters(tmp_path: Path):
    """Manual/signature origin stamps ride on the success upsert (D-01):
    origin and reason persist verbatim, and no stderr ever appears --
    `upsert` has no status_stderr parameter at all, so the column stays
    NULL-by-omission on every success path (D-04)."""
    from jarvis.registry import ORIGIN_MANUAL, Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        returned = registry.upsert("mine", "/repos/mine", "python", "abc123",
                                   "search-only",
                                   status_origin=ORIGIN_MANUAL)
        entry = registry.get("mine")
        assert entry is not None
        assert entry.status_origin == ORIGIN_MANUAL
        assert entry.status_reason is None
        assert entry.status_stderr is None
        # The constructed return carries the stamp too, so callers can
        # assert without a re-read.
        assert returned.status_origin == ORIGIN_MANUAL
        assert returned.status_reason is None
    finally:
        registry.close()


def test_upsert_origin_parameters_replace_a_prior_failure_record(tmp_path: Path):
    """A search-only success after a recorded failure must leave the row
    describing THIS run: signature origin plus the matched reason, with
    the stale failure's stderr cleared to NULL by the conflict list
    (D-04) rather than lingering beside the new origin."""
    from jarvis.registry import ORIGIN_FAILED_HARD, ORIGIN_SIGNATURE, Registry

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.record_failure("mine", "/repos/mine", "java", ORIGIN_FAILED_HARD,
                                "scip-java index failed", "scip-java index failed:\nboom")
        registry.upsert("mine", "/repos/mine", "java", "abc123", "search-only",
                        status_origin=ORIGIN_SIGNATURE,
                        status_reason="matched signature explanation")
        entry = registry.get("mine")
        assert entry is not None
        assert entry.status_origin == ORIGIN_SIGNATURE
        assert entry.status_reason == "matched signature explanation"
        assert entry.status_stderr is None
    finally:
        registry.close()


def test_plain_upsert_leaves_failure_fields_null(tmp_path: Path):
    """Defaults keep D-04 exactly as before the parameters existed: every
    plain success path (indexed/partial/indexing) persists NULL failure
    fields, both in the row and in the constructed return value."""
    registry = Registry(tmp_path / "registry.db")
    try:
        returned = registry.upsert("mine", "/repos/mine", "python", "abc123", "indexed")
        entry = registry.get("mine")
        assert entry is not None
        assert (entry.status_origin, entry.status_reason, entry.status_stderr) == (None, None, None)
        assert (returned.status_origin, returned.status_reason, returned.status_stderr) == (None, None, None)
    finally:
        registry.close()


def test_legacy_choice_columns_are_dropped_by_the_migration(tmp_path: Path):
    """TSI-08 consolidation: a pre-migration registry (search_only +
    fallback_enabled columns present) migrates in one transaction — facts
    copied, obsolete columns dropped, no dual state models. Repeat opens
    are idempotent."""
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE repos (slug TEXT PRIMARY KEY, path TEXT NOT NULL, "
        "language TEXT NOT NULL, commit_sha TEXT, last_indexed TEXT NOT NULL, "
        "status TEXT NOT NULL, scheme_override TEXT, semantic_indexed_at TEXT, "
        "semantic_include TEXT, language_override TEXT, "
        "search_only INTEGER NOT NULL DEFAULT 0, tracked_files INTEGER, "
        "status_origin TEXT, status_reason TEXT, status_stderr TEXT, "
        "fallback_enabled INTEGER)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('old', '/p', 'python', 'abc', "
        "'2026-01-01T00:00:00+00:00', 'indexed', NULL, NULL, NULL, NULL, "
        "0, 42, NULL, NULL, NULL, NULL)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('auto', '/q', 'kotlin', 'def', "
        "'2026-01-02T00:00:00+00:00', 'search-only', NULL, NULL, NULL, NULL, "
        "1, 7, 'signature', 'ABI mismatch', 'scip output', NULL)"
    )
    conn.commit()
    conn.close()

    registry = Registry(db)
    try:
        entry = registry.get("old")
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.tracked_files == 42
        assert entry.scip_enabled is True
        assert entry.scip_state is None
        auto = registry.get("auto")
        assert auto is not None
        # Automatically classified fallback: enabled, failure copied to the
        # stage fields, retry permitted on the next explicit run (TSI-08).
        assert auto.scip_enabled is True
        assert auto.scip_state == "failed"
        assert auto.scip_failure_reason == "ABI mismatch"
        assert auto.scip_failure_stderr == "scip output"
        assert auto.scip_failed_at_sha == "def"
    finally:
        registry.close()

    probe = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in probe.execute("PRAGMA table_info(repos)")}
        assert "search_only" not in cols
        assert "fallback_enabled" not in cols
        assert "scip_enabled" in cols
    finally:
        probe.close()

    # Repeat migration: a no-op that changes nothing (spec TSI-08).
    registry = Registry(db)
    try:
        again = registry.get("auto")
        assert again is not None
        assert again.scip_state == "failed"
        assert again.scip_failure_reason == "ABI mismatch"
    finally:
        registry.close()


def test_set_scip_enabled_roundtrip(tmp_path: Path):
    """The reversible choice (spec TSI-06/TSI-07): the dedicated setter
    updates it directly; upserts carry the resolved value explicitly, so
    the choice only changes when a run says so."""
    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("mine", "/repos/mine", "python", "abc", "indexing")
        assert registry.get("mine").scip_enabled is True
        registry.set_scip_enabled("mine", False)
        assert registry.get("mine").scip_enabled is False
        registry.set_scip_enabled("mine", True)
        assert registry.get("mine").scip_enabled is True
    finally:
        registry.close()


def test_semantic_declined_column_migrates_onto_an_existing_database(tmp_path: Path):
    """SEMA-01/SC2: a phase-3-era registry (fallback_enabled present,
    semantic_declined absent) gains the decline-memory column on first
    open via `_ensure_column`; the legacy row reads NULL → False — never
    answered, so the offer fires again — with existing fields intact."""
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE repos (slug TEXT PRIMARY KEY, path TEXT NOT NULL, "
        "language TEXT NOT NULL, commit_sha TEXT, last_indexed TEXT NOT NULL, "
        "status TEXT NOT NULL, scheme_override TEXT, semantic_indexed_at TEXT, "
        "semantic_include TEXT, language_override TEXT, "
        "search_only INTEGER NOT NULL DEFAULT 0, tracked_files INTEGER, "
        "status_origin TEXT, status_reason TEXT, status_stderr TEXT, "
        "fallback_enabled INTEGER)"
    )
    conn.execute(
        "INSERT INTO repos VALUES ('old', '/p', 'python', 'abc', "
        "'2026-01-01T00:00:00+00:00', 'indexed', NULL, NULL, NULL, NULL, "
        "0, 42, NULL, NULL, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    registry = Registry(db)
    try:
        entry = registry.get("old")
        assert entry is not None
        assert entry.status == "indexed"
        assert entry.tracked_files == 42
        assert entry.scip_enabled is True
        assert entry.semantic_declined is False  # NULL = never answered
    finally:
        registry.close()

    probe = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in probe.execute("PRAGMA table_info(repos)")}
        assert "semantic_declined" in cols
    finally:
        probe.close()


def test_semantic_declined_roundtrip_and_preservation(tmp_path: Path):
    """SEMA-01 memory semantics: the decline bit roundtrips through the
    dedicated setter and survives every plain `upsert` and
    `record_failure` write (neither column list ever names it — the
    tracked_files trap), and dies only with the row via `jarvis forget`
    (locked Area 3)."""
    from jarvis.registry import ORIGIN_FAILED_HARD

    registry = Registry(tmp_path / "registry.db")
    try:
        registry.upsert("mine", "/repos/mine", "python", "abc", "indexing")
        assert registry.get("mine").semantic_declined is False
        registry.set_semantic_declined("mine", True)
        assert registry.get("mine").semantic_declined is True
        # The terminal-write shape: no decline argument rides the upsert.
        registry.upsert("mine", "/repos/mine", "python", "def", "indexed")
        assert registry.get("mine").semantic_declined is True
        # A failed run must not forget the decline either.
        registry.record_failure(
            "mine", "/repos/mine", "python", ORIGIN_FAILED_HARD,
            "scip-python index failed", "scip-python index failed:\nboom",
        )
        assert registry.get("mine").semantic_declined is True
        # The memory dies with the row (locked Area 3).
        assert registry.forget("mine") is True
        assert registry.get("mine") is None
    finally:
        registry.close()


def test_recovery_for_fallback_origin_names_the_self_heal():
    """FALL-01/Area 3: locked wording — names the fix, the reindex verb,
    and the self-heal (full build retries automatically), so a degraded
    row tells the user recovery is one command away."""
    from jarvis.registry import ORIGIN_FALLBACK, recovery_for

    assert recovery_for(_entry(status="degraded", status_origin=ORIGIN_FALLBACK)) == (
        "fix the indexer failure, then `jarvis reindex mine` (full build retries automatically)"
    )


def test_upsert_round_trips_status_stderr_and_plain_upsert_clears_it(tmp_path: Path):
    """FALL-01 carrier + D-04: the degraded terminal write persists the
    complete multi-line failure text verbatim; the next plain successful
    upsert NULL-clears origin/reason/stderr together on the previously
    degraded row (self-heal leaves no stale failure facts)."""
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    text = "scip-python index failed (scip-python index):\nline one\nline two"
    registry = Registry(tmp_path / "registry.db")
    try:
        returned = registry.upsert(
            "mine", "/repos/mine", "python", "abc123", DEGRADED_STATUS,
            status_origin=ORIGIN_FALLBACK,
            status_reason="scip-python index failed (scip-python index)",
            status_stderr=text,
        )
        assert returned.status_stderr == text
        entry = registry.get("mine")
        assert entry is not None
        assert entry.status == DEGRADED_STATUS
        assert entry.status_stderr == text
        assert "\n" in entry.status_stderr  # multi-line text survives whole

        registry.upsert("mine", "/repos/mine", "python", "def456", "indexed")
        healed = registry.get("mine")
        assert healed is not None
        assert healed.status == "indexed"
        assert (healed.status_origin, healed.status_reason, healed.status_stderr) == (
            None, None, None,
        )
    finally:
        registry.close()


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
