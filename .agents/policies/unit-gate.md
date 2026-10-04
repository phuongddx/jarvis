# Policy: the unit gate is the full suite with the native group

**Rule.** "Tests pass" means the CI unit gate passed:

```sh
uv sync --frozen --group native
uv run --frozen pytest -m "not integration" -rs
```

A focused run of one test file is evidence for that file only. Never
report it as the gate.

- Report the result line exactly: passed, skipped, and deselected counts.
- Skips from missing `lancedb` (`tests/test_semantic.py`) are acceptable
  only when the diff does not touch `src/jarvis/semantic.py`,
  `chunker.py`, or `embeddings.py`. If it does, also sync
  `--extra semantic` and run the gate again.
- Real binaries belong in `-m integration` tests. Run the integration
  tests that cover a change when the change touches an indexer or
  external-binary path.

**Why.** Without `--group native`, collection fails on the packaging
tests (`No module named 'PyInstaller'`). In the `agent-tool-guidance`
work, an implementer reported 67 passed from one file while the full
gate had never run.

**Check.** The report quotes a result line from the full command above,
run on the commit being proposed.
