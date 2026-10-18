import json
import sqlite3

from tests.fixtures.carry_db import build_prev_snapshot


def _meta(**over):
    from jarvis.index_reader import IndexMetadata

    base = dict(project="_", repo="demo", branch="_", commit_sha="new111",
                published_at=None, scip_mode="available", carry_depth=0)
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
        # b.py vanished from the capture (deletion) AND new.py is new:
        assert changed_paths(conn, {"a.py": "ha1", "new.py": "x"}) == {"b.py", "new.py"}
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
        assert conn.execute(
            "SELECT COUNT(*) FROM global_symbols").fetchone()[0] == 2
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
            "JOIN documents d ON d.id = c.document_id"
        ).fetchall() == [("b.py", "prev000")]
    finally:
        conn.close()


def test_published_metadata_reads_pointer_target(tmp_path):
    from jarvis.carry import pointer_target, published_metadata

    slug_dir = tmp_path / "slug"
    slug_dir.mkdir()
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
