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
from jarvis.scip_decoder import SymbolRoles

CARRY_DEPTH_CAP = 5
CARRY_CLOSURE_THRESHOLD = 0.60
CARRYABLE_STATES = frozenset({"available", "carried"})
# Spec enum for a watch run's full-refresh decision. `no-previous` is
# deliberately excluded: a first index is not a refresh decision, so it
# surfaces as null (controller ruling, Task 7 fix 1).
FULL_REFRESH_REASONS = frozenset({"not-carryable", "depth-cap", "threshold"})


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


def changed_paths(prev: sqlite3.Connection,
                  source_hashes: dict[str, str]) -> set[str]:
    """Stored-hash diffs from this capture + new captured paths."""
    rows = prev.execute(
        "SELECT file_path, file_hash FROM syntax_files").fetchall()
    stored = {path: digest for path, digest in rows}
    return ({p for p, d in rows if source_hashes.get(p) != d}
            | (set(source_hashes) - set(stored)))


def closure_paths(prev: sqlite3.Connection, changed: set[str]) -> set[str]:
    """Docs mentioning any symbol DEFINED in `changed` docs. Outer join
    is role-agnostic: any mention of a changed-file symbol stales a doc.
    Definition detection uses the raw role BITMASK (query.py contract),
    never exact equality."""
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
        f"  WHERE (m2.role & ?) != 0 AND c2.document_id IN ({id_marks})"
        f") AND c.document_id NOT IN ({id_marks})",
        (SymbolRoles.DEFINITION, *ids, *ids)).fetchall()
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
        # Enforcement is per-connection in SQLite: without this, a wrong
        # delete order below would corrupt silently instead of failing.
        conn.execute("PRAGMA foreign_keys = ON")
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
                    f"SELECT id FROM chunks WHERE document_id IN ({id_marks}))",
                    args)
                conn.execute(
                    f"DELETE FROM chunks WHERE document_id IN ({id_marks})", args)
                conn.execute(
                    f"DELETE FROM defn_enclosing_ranges WHERE document_id IN ({id_marks})",
                    args)
                conn.execute(
                    f"DELETE FROM documents WHERE id IN ({id_marks})", args)
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
            "scip_definitions = (SELECT COUNT(*) FROM mentions "
            "WHERE (role & ?) != 0), "
            "scip_references = (SELECT COUNT(*) FROM mentions "
            "WHERE (role & ?) = 0), "
            "scip_calls = 0, scip_types = 0",
            (SymbolRoles.DEFINITION, SymbolRoles.DEFINITION))
        conn.commit()
    finally:
        conn.close()
