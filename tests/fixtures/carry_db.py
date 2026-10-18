"""Minimal previous-snapshot fixture: b.py references symbol defined in a.py.

Foreign keys are real (REFERENCES clauses): the carry lane's delete order is
only verifiable when the destination connection enforces them."""

import sqlite3


def build_prev_snapshot(dest) -> None:
    conn = sqlite3.connect(dest)
    try:
        conn.executescript("""
        CREATE TABLE documents (id INTEGER PRIMARY KEY, language TEXT,
          relative_path TEXT NOT NULL UNIQUE, position_encoding TEXT, text TEXT);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY,
          document_id INTEGER NOT NULL REFERENCES documents(id),
          chunk_index INTEGER NOT NULL, start_line INTEGER NOT NULL,
          end_line INTEGER NOT NULL, occurrences BLOB NOT NULL);
        CREATE TABLE global_symbols (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL UNIQUE,
          display_name TEXT, kind INTEGER, documentation TEXT, signature BLOB,
          enclosing_symbol TEXT, relationships BLOB);
        CREATE TABLE mentions (chunk_id INTEGER NOT NULL REFERENCES chunks(id),
          symbol_id INTEGER NOT NULL REFERENCES global_symbols(id),
          role INTEGER NOT NULL, PRIMARY KEY (chunk_id, symbol_id, role));
        CREATE TABLE defn_enclosing_ranges (id INTEGER PRIMARY KEY,
          document_id INTEGER REFERENCES documents(id),
          symbol_id INTEGER REFERENCES global_symbols(id),
          start_line INTEGER, start_char INTEGER,
          end_line INTEGER, end_char INTEGER);
        CREATE TABLE jarvis_snapshot (singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          format_version INTEGER, generation TEXT, commit_sha TEXT, published_at TEXT,
          source_hash TEXT, scip_state TEXT, scip_outlines INTEGER, scip_definitions INTEGER,
          scip_references INTEGER, scip_calls INTEGER, scip_types INTEGER, syntax_counts TEXT);
        CREATE TABLE syntax_files (
          file_path TEXT PRIMARY KEY, file_hash TEXT NOT NULL);
        """)
        conn.executemany(
            "INSERT INTO documents (id, relative_path) VALUES (?, ?)",
            [(1, "a.py"), (2, "b.py")],
        )
        conn.executemany(
            "INSERT INTO chunks (id, document_id, chunk_index, start_line, "
            "end_line, occurrences) VALUES (?, ?, ?, ?, ?, ?)",
            [(10, 1, 0, 0, 0, b""), (20, 2, 0, 0, 0, b"")],
        )
        conn.executemany(
            "INSERT INTO global_symbols (id, symbol) VALUES (?, ?)",
            [(100, "demo a.A."), (101, "demo b.B.")],
        )
        conn.executemany(
            "INSERT INTO mentions VALUES (?, ?, ?)",
            [(10, 100, 3),   # a.py defines A (converter may emit Definition|Import)
             (20, 100, 0),   # b.py references A
             (20, 101, 1)],  # b.py defines B
        )
        conn.execute(
            "INSERT INTO defn_enclosing_ranges "
            "(document_id, symbol_id, start_line, start_char, end_line, end_char) "
            "VALUES (1, 100, 1, 0, 2, 0)"
        )
        conn.execute(
            "INSERT INTO jarvis_snapshot VALUES (1, 1, 'g0', 'prev000', "
            "'2026-10-18T00:00:00Z', 'hash0', 'available', 2, 2, 1, 0, 0, '{}')"
        )
        conn.execute("INSERT INTO syntax_files VALUES ('a.py', 'ha1')")
        conn.execute("INSERT INTO syntax_files VALUES ('b.py', 'hb1')")
        conn.commit()
    finally:
        conn.close()
