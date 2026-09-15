# Semantic Search Install UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `jarvis install-semantic` pre-download the `BAAI/bge-m3` embedding model (one time, globally cached), give Homebrew users a guided `semanticSearch` error, and make the plugin setup skill ask about semantic search.

**Architecture:** Three independent changes across two repos. (1) jarvis CLI: a public `EmbeddingModel.preload()` warms the Hugging Face cache; `_cmd_install_semantic` calls it after dependency sync, non-fatally. (2) jarvis frozen-runtime hint: point Homebrew users at the README's source-build section — deliberately **without** mentioning `uv` (locked by existing test). (3) plugin repo setup skill: agent asks the user a yes/no semantic question after Homebrew install; accepted → guided source setup. The `jarvis index` interactive offer is untouched — its accept path already downloads the model during the semantic rerun.

**Tech Stack:** Python 3.12 stdlib + argparse CLI (`src/jarvis/`), pytest, sentence-transformers/Hugging Face Hub cache (deferred imports), Homebrew frozen runtime (`jarvis.runtime.is_frozen`), Claude plugin skill markdown.

**Spec:** this plan's "Approved design" section (below) — distilled from the 2026-09-15 brainstorming session; no separate spec file.

## Approved design

1. **Model**: `BAAI/bge-m3`, local-only. No cloud/OpenRouter backends.
2. **`jarvis install-semantic` scope**: deps (`uv sync --extra semantic --extra watch --group native`) **+ pre-download model** (4.3 GB, one time, `~/.cache/huggingface/`, shared across repos).
3. **`jarvis index` consent**: unchanged — per-repo decline memory (`SEMA-01`), TTY-gated offer; accept path downloads model during semantic rerun.
4. **Homebrew `semanticSearch` error**: guided pointer to README source-build section; **never mentions `uv`**; tool stays registered (10 tools, never 9).
5. **Plugin setup skill**: after Homebrew install, ask about semantic (source checkout + 4.3 GB). Yes → guided source setup + MCP command override. No → continue. Skip if source-mode semantic already present.

## Global Constraints

- Python ≥3.12,<3.15; `from __future__ import annotations`; `X | None`, `list[T]` typing.
- Frozen-distribution hint must never contain the substring `uv` (existing lock: `test_frozen_semantic_install_hint_never_mentions_uv`).
- Tests never download the model — monkeypatch `jarvis.embeddings.EmbeddingModel` or fake `sentence_transformers`; never hit network.
- stdout stays machine-parseable; progress/warnings to stderr.
- Model preload failure is **non-fatal** (rc 0 + warning) — dependencies are installed; next index retries the download.
- Conventional Commits, lowercase imperative subjects; PRs against `main`; `uv run pytest -m "not integration"` green.
- Do not refactor unrelated code; surgical diffs only.

---

## Phase 1 — jarvis repo (`phuongddx/jarvis`)

### Task 1: Sync local main to origin/main

**Files:** none (git operation only).

Local `main` is `ahead 1, behind 4`. The ahead commit `c8d3ca7` (semantic-install plan doc) is byte-identical to the version merged in PR #65 — verified `git diff c8d3ca7 origin/main -- plans/2026-09-15-semantic-install-command.md` is empty. Untracked files (plans/reports, diagrams) are not touched by reset.

- [ ] **Step 1: Verify state is safe to reset**

```bash
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis fetch origin
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis status -sb
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis log --oneline main ^origin/main
```

Expected: `## main...origin/main [ahead 1, behind 4]`; exactly one local commit `c8d3ca7 docs(plans): add semantic install command plan`; no staged/modified tracked files (untracked OK).

- [ ] **Step 2: Reset to remote and verify**

```bash
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis reset --hard origin/main
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis log --oneline -1
```

Expected: `9c1224e Merge pull request #65 from phuongddx/feat/semantic-install-command`.

### Task 2: `EmbeddingModel.preload()` public method

**Files:**
- Modify: `src/jarvis/embeddings.py` (inside `class EmbeddingModel`, immediately after `_load`, ~line 122)
- Test: `tests/test_embeddings.py` (append at end)

**Interfaces:**
- Produces: `EmbeddingModel.preload(self) -> None` — calls `self._load()`, so the model downloads through the existing pinned-revision path. Raises whatever `_load` raises (import failure → `SemanticExtraMissingError`; network errors propagate). Callers decide fatality. Consumed by Task 3.

- [ ] **Step 1: Write the failing test** — append to `tests/test_embeddings.py`:

```python
def test_preload_loads_model_through_pinned_revision(monkeypatch):
    holder = _install_fake(monkeypatch)
    model = EmbeddingModel()
    model.preload()
    assert holder["model"] is model._model
    assert holder["model"].model_name == "BAAI/bge-m3"
    assert holder["model"].revision == embeddings.DEFAULT_REVISION
```

- [ ] **Step 2: Run it, verify it fails**

```bash
uv run pytest tests/test_embeddings.py::test_preload_loads_model_through_pinned_revision -v
```

Expected: FAIL — `AttributeError: 'EmbeddingModel' object has no attribute 'preload'`.

- [ ] **Step 3: Implement** — in `src/jarvis/embeddings.py`, directly after the `_load` method body closes (before `def _encode`):

```python
    def preload(self) -> None:
        """Download/load the model weights into the shared Hugging Face
        cache (~4.3 GB for the default model, one time, shared across
        repos). Used by `jarvis install-semantic` so the first index does
        not pay the download cost. Raises whatever _load raises; callers
        decide whether that is fatal."""
        self._load()
```

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/test_embeddings.py -v
```

Expected: all PASS (existing tests unaffected — additive method).

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/embeddings.py tests/test_embeddings.py
git commit -m "feat(embeddings): add public preload for model pre-download"
```

### Task 3: `install-semantic` pre-downloads the model

**Files:**
- Modify: `src/jarvis/index_cli.py` — replace `_cmd_install_semantic` (~lines 802-868) with two helpers + slim command; no public signatures change
- Test: `tests/test_index_cli.py` — update 2 existing tests (`test_install_semantic_reports_already_installed` ~4532, `test_install_semantic_runs_locked_sync` ~4567), add 1 new test; all other `test_install_semantic_*` unchanged and must still pass

**Interfaces:**
- Consumes: `EmbeddingModel.preload()` from Task 2 (imported inside the helper — deferred, matching the optional-extra pattern).
- Produces: `_sync_semantic_extra(project_root: Path) -> bool` and `_preload_embedding_model() -> bool` (module-private, monkeypatch-isolated). CLI behavior: both fresh-install and already-installed paths end with the model stage; preload failure warns on stderr and returns rc 0.

- [ ] **Step 1: Update the two existing tests first (they encode the new contract)**

Replace `test_install_semantic_reports_already_installed` with:

```python
def test_install_semantic_reports_already_installed(monkeypatch, capsys):
    import argparse

    from jarvis import index_cli

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: False)

    preload_calls: list[int] = []

    class _FakeModel:
        def preload(self):
            preload_calls.append(1)

    monkeypatch.setattr("jarvis.embeddings.EmbeddingModel", _FakeModel)

    def forbidden():
        raise AssertionError("installed support must not trigger another sync")

    monkeypatch.setattr(index_cli.shutil, "which", forbidden)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == (
        "semantic support already installed\nembedding model ready\n"
    )
    assert "downloading embedding model" in captured.err
    assert preload_calls == [1]
```

In `test_install_semantic_runs_locked_sync`, add after the existing `importlib.invalidate_caches` monkeypatch:

```python
    preload_calls: list[int] = []

    class _FakeModel:
        def preload(self):
            preload_calls.append(1)

    monkeypatch.setattr("jarvis.embeddings.EmbeddingModel", _FakeModel)
```

and change its final assertions to:

```python
    assert rc == 0
    assert calls == [
        (
            [uv_path, "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"],
            root,
            600,
            sync_options,
        )
    ]
    assert invalidations == [1]
    assert preload_calls == [1]
    assert captured.out == (
        "semantic support installed\nembedding model ready\n"
    )
    assert captured.err.startswith("installing semantic support...\n")
    assert "downloading embedding model" in captured.err
```

- [ ] **Step 2: Add the failure-path test** — append after `test_install_semantic_runs_locked_sync`:

```python
def test_install_semantic_preload_failure_is_nonfatal(monkeypatch, capsys, tmp_path):
    import argparse
    import subprocess as subprocess_module

    from jarvis import index_cli

    root = tmp_path / "jarvis"
    root.mkdir()
    (root / "pyproject.toml").write_text("", encoding="utf-8")

    def fake_run(cmd, *, cwd, timeout, capture_output, text, errors):
        return subprocess_module.CompletedProcess(cmd, 0, stdout="", stderr="")

    class _BoomModel:
        def preload(self):
            raise RuntimeError("network down")

    monkeypatch.setattr(index_cli.runtime, "is_frozen", lambda: False)
    monkeypatch.setattr(index_cli, "_semantic_project_root", lambda: root)
    monkeypatch.setattr(index_cli.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(index_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(index_cli, "_semantic_extra_missing", lambda: False)
    monkeypatch.setattr("jarvis.embeddings.EmbeddingModel", _BoomModel)

    rc = index_cli._cmd_install_semantic(argparse.Namespace())
    captured = capsys.readouterr()

    assert rc == 0
    assert "warning: embedding model download failed (network down)" in captured.err
    assert "will download on the next index" in captured.err
    assert "embedding model ready" not in captured.out
```

- [ ] **Step 3: Run tests, verify the three fail**

```bash
uv run pytest tests/test_index_cli.py -k install_semantic -v
```

Expected: the two updated tests + new test FAIL (stdout missing `embedding model ready`); frozen / missing-uv / missing-checkout / sync-failure tests still PASS.

- [ ] **Step 4: Implement** — in `src/jarvis/index_cli.py`, extract the sync body into a helper and add the preload helper above `_cmd_install_semantic`, then slim the command. Final shape of all three:

```python
def _sync_semantic_extra(project_root: Path) -> bool:
    """Run the locked `uv sync` for the semantic extra. True only when
    the sync exits 0 and both semantic imports resolve afterwards."""
    uv = shutil.which("uv")
    if uv is None:
        print("semantic support installation requires uv", file=sys.stderr)
        return False
    print("installing semantic support...", file=sys.stderr)
    try:
        result = subprocess.run(
            [uv, "sync", "--extra", "semantic", "--extra", "watch", "--group", "native"],
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        print("semantic support installation failed", file=sys.stderr)
        return False
    if result.returncode != 0:
        print("semantic support installation failed", file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return False
    importlib.invalidate_caches()
    if _semantic_extra_missing():
        print(
            "semantic support installation completed but lancedb or "
            "sentence_transformers is unavailable",
            file=sys.stderr,
        )
        return False
    return True


def _preload_embedding_model() -> bool:
    """Warm the Hugging Face cache for the configured embedding model
    (~4.3 GB for the default, one time, shared across repos). Non-fatal
    by design: dependencies are already installed, and the next index
    retries missing weights through SentenceTransformer."""
    print(
        "downloading embedding model (one time, ~4.3 GB, shared across repos)...",
        file=sys.stderr,
    )
    try:
        from jarvis.embeddings import EmbeddingModel
        EmbeddingModel().preload()
    except Exception as exc:
        print(
            f"warning: embedding model download failed ({exc}); dependencies "
            "are installed and the model will download on the next index",
            file=sys.stderr,
        )
        return False
    print("embedding model ready")
    return True


def _cmd_install_semantic(args: argparse.Namespace) -> int:
    """Install semantic dependencies and pre-download the model in a
    source checkout without shell work."""
    del args
    if runtime.is_frozen():
        print(
            "semantic support is not installable — the Homebrew binary "
            "distribution excludes semantic dependencies; use a source "
            "checkout (see the README's Source-build semantic search section)",
            file=sys.stderr,
        )
        return 1

    project_root = _semantic_project_root()
    if not (project_root / "pyproject.toml").is_file():
        print(
            "semantic support installation requires a jarvis source checkout "
            f"(missing {project_root / 'pyproject.toml'})",
            file=sys.stderr,
        )
        return 1

    if _semantic_extra_missing():
        if not _sync_semantic_extra(project_root):
            return 1
        print("semantic support installed")
    else:
        # Deps present does not imply weights cached — verify the model too.
        print("semantic support already installed")

    _preload_embedding_model()
    return 0
```

Note: the frozen-refusal message now appends source-checkout guidance — contains no `uv`; the existing `"Homebrew binary distribution" in err` assertion still passes.

- [ ] **Step 5: Run tests, verify pass**

```bash
uv run pytest tests/test_index_cli.py -k install_semantic -v
```

Expected: all `install_semantic` tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/jarvis/index_cli.py tests/test_index_cli.py
git commit -m "feat(cli): pre-download embedding model in install-semantic"
```

### Task 4: Guided frozen `semanticSearch` hint

**Files:**
- Modify: `src/jarvis/embeddings.py` — frozen branch of `semantic_install_hint()` (~lines 36-40)
- Test: `tests/test_index_cli.py` — extend `test_frozen_semantic_install_hint_never_mentions_uv` (~line 4459); no new test file

**Interfaces:**
- Produces: frozen hint string guiding to the README section. **Constraint:** must NOT contain substring `uv`; must contain the repo URL and the exact README section name `Source-build semantic search`.

- [ ] **Step 1: Extend the existing test** — in `test_frozen_semantic_install_hint_never_mentions_uv`, add after the existing frozen assertions:

```python
    assert "github.com/phuongddx/jarvis" in frozen_hint
    assert "Source-build semantic search" in frozen_hint
```

The existing `assert "uv" not in frozen_hint` stays.

- [ ] **Step 2: Run it, verify it fails**

```bash
uv run pytest tests/test_index_cli.py::test_frozen_semantic_install_hint_never_mentions_uv -v
```

Expected: FAIL — current hint has no URL or section name.

- [ ] **Step 3: Implement** — replace the frozen branch in `semantic_install_hint()`:

```python
def semantic_install_hint() -> str:
    if runtime.is_frozen():
        return (
            "semantic search is not included in the Homebrew binary "
            "distribution; to enable it, clone "
            "https://github.com/phuongddx/jarvis and follow the "
            "'Source-build semantic search' section of its README"
        )
    return _INSTALL_HINT
```

Sanity-check the no-`uv` lock: no word above contains the substring `uv`.

- [ ] **Step 4: Run tests, verify pass**

```bash
uv run pytest tests/test_index_cli.py -k "install_hint or install_semantic" -v
```

Expected: PASS, including `test_install_semantic_refuses_frozen_build`.

- [ ] **Step 5: Commit**

```bash
git add src/jarvis/embeddings.py tests/test_index_cli.py
git commit -m "fix(semantic): guide homebrew users to source-build setup"
```

### Task 5: README — model pre-download + Homebrew pointer

**Files:**
- Modify: `README.md` — the two `<details>` blocks: "Not included" (~line 118) and "Source-build semantic search" (~line 125)

**Interfaces:** none (docs).

- [ ] **Step 1: Edit the "Not included" block** — replace its body with:

```markdown
`semanticSearch` is registered for MCP compatibility but is excluded from the
standalone distribution. It returns a Homebrew-specific unavailability error
that points to the source-build steps below; lexical search and symbol search
remain available.
```

- [ ] **Step 2: Edit the "Source-build semantic search" block** — replace the sentence `The first embedding run downloads the configured model.` with:

```markdown
`install-semantic` also pre-downloads the embedding model (`BAAI/bge-m3`,
~4.3 GB, one time) into the shared Hugging Face cache
(`~/.cache/huggingface/`); every indexed repo reuses it. If the download is
interrupted, the next `jarvis index` retries it automatically.
```

- [ ] **Step 3: Eyeball both blocks render intact**

```bash
sed -n '/<summary>Not included<\/summary>/,/<\/details>/p' README.md
sed -n '/<summary>Source-build semantic search<\/summary>/,/<\/details>/p' README.md
```

Expected: both blocks show the new text; markdown fences intact.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(readme): document model pre-download and homebrew guidance"
```

### Task 6: Full gate + live smoke + PR

**Files:** none (verification only).

- [ ] **Step 1: Unit gate**

```bash
uv run pytest -m "not integration" -rs
```

Expected: all pass (baseline on `9c1224e` was 934 passed, 20 deselected).

- [ ] **Step 2: Version lockstep guard**

```bash
uv run python scripts/check_versions.py
```

Expected: `versions consistent`.

- [ ] **Step 3: Live smoke** — the model is already cached on this machine (`~/.cache/huggingface/hub/models--BAAI--bge-m3`, 4.3G), so this exercises the already-installed → preload path without a download:

```bash
uv run jarvis install-semantic
du -sh ~/.cache/huggingface/hub/models--BAAI--bge-m3
```

Expected: stdout `semantic support already installed` + `embedding model ready`; exit 0.

- [ ] **Step 4: PR**

```bash
git push -u origin feat/semantic-install-ux
gh pr create --title "feat: semantic install model pre-download and guided homebrew error" \
  --body "Implements the approved semantic-install-ux design: install-semantic pre-downloads bge-m3 (one-time global cache); frozen semanticSearch hint points to README source-build section (no uv mention, locked by test); README updated. Baseline 934 passed."
```

Merge when CI green (user decision).

---

## Phase 2 — plugin repo (`jarvis-intelligence/jarvis-index`)

Run Phase 2 only after Phase 1 is merged — the skill text references `install-semantic`'s new pre-download behavior.

### Task 7: Sync plugin repo local main

**Files:** none (git operation only).

Local is `behind 5` (v0.11.0 Homebrew launcher work; remote at `12f85fa`). `.planning/config.json` shows modified locally — planning state, not source; reset is acceptable.

- [ ] **Step 1: Sync**

```bash
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis-index fetch origin
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis-index status -sb
git -C /Users/ddphuong/Projects/jarvis-ai/jarvis-index reset --hard origin/main
```

Expected: HEAD at `12f85fa Merge pull request #16 ... fix/homebrew-plugin-launcher`.

### Task 8: Setup skill asks about semantic search

**Files:**
- Modify: `plugin/skills/jarvis-setup/SKILL.md` — step 2, replace the sentence ending with `"`semanticSearch` dependencies are not available in this binary distribution."` (~line 34); also check step 6 (Troubleshooting) and `references/` for the same stale wording

**Interfaces:**
- Consumes: Phase 1 behavior — `uv run jarvis install-semantic` pre-downloads the model; frozen `semanticSearch` error points to README.
- Produces: agent-facing decision block (no code interfaces).

- [ ] **Step 1: Replace the flat sentence in step 2 with this block:**

```markdown
### Optional: semantic search

Homebrew jarvis cannot include `semanticSearch` (the frozen binary excludes its
Python dependencies). After the Homebrew install succeeds, **ask the user**:

> Semantic search is optional. Enabling it requires a jarvis source checkout
> and a one-time ~4.3 GB model download (`BAAI/bge-m3`, cached globally and
> shared by all repos). Enable it now? (y/N)

- **Skip the question** when source-mode jarvis with the semantic extra is
  already active: `python -c "import lancedb, sentence_transformers"` succeeds
  in the jarvis venv the MCP server runs from.
- **If declined:** continue setup normally. The offer reappears per-repo on the
  next interactive `jarvis index` in a source checkout; do not persist a global
  opt-out.
- **If accepted**, check `uv` is installed (`uv --version`; install from
  https://docs.astral.sh/uv/ if missing), then guide:

  ```bash
  git clone https://github.com/phuongddx/jarvis.git
  cd jarvis
  uv sync --extra semantic --extra watch --group native
  uv run jarvis install-semantic   # also pre-downloads the model
  ```

  Then point the MCP client at the source server (overriding the Homebrew
  binary):

  ```bash
  codex mcp add jarvis -- "$(pwd)/.venv/bin/jarvis-server"
  # claude mcp add jarvis --scope user -- "$(pwd)/.venv/bin/jarvis-server"
  ```

  Re-run `jarvis status <slug>` from the checkout to confirm the
  `semanticSearch` capability before reporting success.
```

- [ ] **Step 2: Grep for stale copies and fix**

```bash
grep -rn "dependencies are not available in this binary distribution" plugin/
grep -rni "semantic" plugin/skills/jarvis-setup/SKILL.md plugin/skills/jarvis-setup/references/
```

Expected: the step-2 sentence is gone; update any troubleshooting/references line that still says semantic is simply "not available" to reference the optional-setup block instead.

- [ ] **Step 3: Commit**

```bash
git add plugin/skills/jarvis-setup
git commit -m "docs(skill): ask about semantic search during setup"
```

### Task 9: Plugin patch release v0.11.1

**Files:**
- Modify: `plugin/.claude-plugin/plugin.json` — `"version": "0.11.0"` → `"0.11.1"`

**Interfaces:** none.

- [ ] **Step 1: Find version lockstep references before bumping**

```bash
grep -rn '0\.11\.0' plugin/ package.json docs/ --include='*.json' --include='*.md' | grep -v node_modules
```

Bump every intentional lockstep hit to `0.11.1` (docs-only release; no `mcp.json` change).

- [ ] **Step 2: Commit and open PR**

```bash
git add -A
git commit -m "chore(release): v0.11.1"
```

Plugin CI includes the `claude plugin validate --strict` gate — must be green before tagging.

- [ ] **Step 3: Tag and release** (after PR merge, following that repo's established flow):

```bash
git tag v0.11.1 && git push origin v0.11.1
```

Create the GitHub Release from the tag; then install the plugin from the release and run the setup skill once — confirm the question appears after Homebrew install and the declined path completes without branching.

---

## Out of scope (locked)

- No cloud/OpenRouter embedding backend (any provider).
- No global semantic opt-out flag.
- No model choice beyond `BAAI/bge-m3`.
- No removal/registration change for `semanticSearch` in Homebrew builds.
- No changes to `SEMA-01` decline memory, the TTY gate, or `_install_semantic_extra` (the index-offer path downloads the model during its semantic rerun already).

## Rollback

- Phase 1: three independent commits — `git revert` per commit.
- Phase 2: docs + version bump — revert the skill edit and cut `0.11.2` if the question harms onboarding.
