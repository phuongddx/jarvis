# Homebrew Release Hardening And Plugin Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the post-release formula/CI audit debt and make the public Codex/Claude/Cursor plugin launch the Homebrew-installed `jarvis-server` at plugin version `0.11.0`.

**Architecture:** The source repo owns formula generation and release-workflow hardening; the external `jarvis-index` repo owns the plugin manifests, MCP launch configuration, docs, and plugin tag. Formula cleanup is test-driven, the workflow audit becomes gating, all mutable GitHub Actions are SHA-pinned, and the plugin moves from `uvx jarvis-mcp` to the Homebrew-installed `jarvis-server`.

**Tech Stack:** Python 3.12, uv, pytest, GitHub Actions, Homebrew, Node.js 22, GitHub CLI, fine-grained GitHub PAT, Git signed tags.

**Spec:** [`docs/superpowers/specs/2026-09-13-homebrew-standalone-binary-distribution-design.md`](../specs/2026-09-13-homebrew-standalone-binary-distribution-design.md), audited against the historical execution record in [`2026-09-14-homebrew-tap-release-enablement.md`](2026-09-14-homebrew-tap-release-enablement.md).

## Global Constraints

- Source repository checkout: `/Users/ddphuong/Projects/jarvis-ai/jarvis`.
- Plugin repository checkout: `/Users/ddphuong/Projects/jarvis-ai/jarvis-index`.
- Do not reprint, commit, pipe to a log, or persist the value of `JARVIS_TAP_TOKEN`.
- The replacement `JARVIS_TAP_TOKEN` must be a fine-grained PAT with repository access limited to `jarvis-intelligence/homebrew-jarvis`; permissions are exactly Contents: Read and write, Metadata: Read.
- Current jarvis release stays `0.11.0`; do not bump `pyproject.toml`, `server.json`, or `uv.lock` in this hardening plan.
- Plugin manifests and plugin release version are exactly `0.11.0`.
- The plugin tag is `v0.11.0` in `jarvis-intelligence/jarvis-index`; do not use `plugin-v0.11.0` because `scripts/check-plugin.mjs` derives `v${version}` and existing plugin tags already use the plain form.
- The plugin advertises exactly 10 MCP tools: `documentSymbols`, `goToDefinition`, `findReferences`, `callHierarchy`, `typeHierarchy`, `getIndexStatus`, `indexRepo`, `searchCode`, `semanticSearch`, and `blastRadius`.
- Both plugin MCP configs must be byte-identical and contain only `{"mcpServers":{"jarvis":{"command":"jarvis-server"}}}`.
- `semanticSearch` remains registered for MCP compatibility but is unavailable in the Homebrew distribution; docs must not tell plugin users to install `jarvis-mcp[semantic]`.
- Homebrew is the primary user installation. Plugin docs may reference `setup.sh` only for optional language-indexer tooling, never as the way to install jarvis itself.
- Formula fixes must be made only in `scripts/generate_homebrew_formula.py`; never hand-edit a published `Formula/jarvis.rb`.
- Replace `Formula["universal-ctags"].opt_bin/"ctags"` with `formula_opt_bin("universal-ctags")/"ctags"`, and use the Ruby octal literal `0755`.
- Remove the formula's redundant `version` stanza and place each `sha256` immediately after its `url`.
- `brew audit --formula "$JARVIS_TAP/jarvis"` must become a gating workflow step with no `|| true`.
- SHA-pin every GitHub Action in `.github/workflows/*.yml` using the exact mapping in Task 3.
- Do not commit unrelated untracked files under `diagrams/`, `plans/`, or any other pre-existing user workspace path.
- Conventional Commits; lowercase imperative subjects. Source PRs require `uv run pytest -m "not integration" -rs` green.

---

### Task 1: Rotate the tap-publishing token to least privilege

**Files:**
- No repository files changed.
- External mutation: GitHub Actions secret `JARVIS_TAP_TOKEN` in `phuongddx/jarvis`.

**Interfaces:**
- Consumes: GitHub account permission to create a fine-grained PAT and update repository Actions secrets.
- Produces: `JARVIS_TAP_TOKEN` backed by a token that can push only to `jarvis-intelligence/homebrew-jarvis`.

- [ ] **Step 1: Record the current secret timestamp**

```bash
gh secret list --repo phuongddx/jarvis --json name,updatedAt \
  --jq '.[] | select(.name == "JARVIS_TAP_TOKEN") | .updatedAt' \
  > /tmp/jarvis-tap-token-before.txt
cat /tmp/jarvis-tap-token-before.txt
```

Expected: one ISO-8601 timestamp is printed and saved. If the command fails because the account cannot read secrets, stop; do not use a broader token to inspect it.

- [ ] **Step 2: Create the replacement token in GitHub without copying it into shell history**

Open <https://github.com/settings/personal-access-tokens/new> in a browser and create a fine-grained token with:

- Token name: `jarvis-homebrew-tap-publish`.
- Expiration: the organization's shortest practical PAT lifetime.
- Repository access: Only select repositories.
- Selected repository: `jarvis-intelligence/homebrew-jarvis`, and no other repository.
- Repository permissions: `Contents` = Read and write; `Metadata` = Read-only.
- No organization permissions.

Copy the token only into the browser clipboard for the next interactive prompt. Do not put it in a file, environment assignment, issue, chat message, or command argument.

- [ ] **Step 3: Update the Actions secret interactively**

```bash
gh secret set JARVIS_TAP_TOKEN --repo phuongddx/jarvis
```

At GitHub CLI's prompt, paste the token from the browser clipboard and submit it. GitHub CLI writes the secret directly; this avoids putting the token into shell history.

- [ ] **Step 4: Verify the secret timestamp changed**

```bash
AFTER="$(gh secret list --repo phuongddx/jarvis --json name,updatedAt \
  --jq '.[] | select(.name == "JARVIS_TAP_TOKEN") | .updatedAt')"
test "$AFTER" != "$(cat /tmp/jarvis-tap-token-before.txt)"
printf 'JARVIS_TAP_TOKEN updated at %s\n' "$AFTER"
rm -f /tmp/jarvis-tap-token-before.txt
```

Expected: the timestamp differs from the saved baseline. No token value is printed.

- [ ] **Step 5: Revoke the former broad token**

In the GitHub browser tab used in Step 2, revoke the old broad GitHub CLI token that had previously been stored as `JARVIS_TAP_TOKEN`. Confirm the revocation before considering this task complete.

Expected: the old token is inactive. This task intentionally does not trigger a release solely to test the publish path; Tasks 2–3 cover static and local validation, and the next real release is the publish-path integration test.

---

### Task 2: Clean up formula warnings

**Files:**
- Modify: `scripts/generate_homebrew_formula.py:57-112`
- Test: `tests/test_generate_homebrew_formula.py:23-45`

**Interfaces:**
- Consumes: `generator.render(version: str, release_tag: str, checksums: dict[str, str]) -> str`.
- Produces: Homebrew DSL whose resource blocks use `url` then `sha256`, no explicit `version`, `formula_opt_bin` for ctags, and `chmod 0755`.

- [ ] **Step 1: Create the source hardening branch**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c fix/homebrew-audit-hardening
```

Expected: branch starts at `origin/main`; the only dirty paths are pre-existing unrelated untracked files.

- [ ] **Step 2: Write the failing formula contract tests**

In `tests/test_generate_homebrew_formula.py`, replace `test_render_contains_every_platform_url_and_checksum` and `test_render_installs_both_names_and_ctags_shim` with:

```python
def test_render_contains_every_platform_url_and_checksum():
    checksums = _checksums()
    formula = generator.render("0.11.0", "v0.11.0", checksums)

    assert 'version "' not in formula
    assert 'depends_on "universal-ctags"' in formula
    for platform, digest in checksums.items():
        url = (
            "https://github.com/jarvis-intelligence/homebrew-jarvis"
            f"/releases/download/v0.11.0/jarvis_0.11.0_{platform}.tar.gz"
        )
        assert f'url "{url}"\n  sha256 "{digest}"' in formula


def test_render_installs_both_names_and_ctags_shim():
    formula = generator.render("0.11.0", "v0.11.0", _checksums())

    assert 'libexec.install Dir["libexec/*"]' in formula
    assert 'Dir["jarvis/libexec/*"]' not in formula
    assert 'bin.install_symlink libexec/"jarvis" => "jarvis"' in formula
    assert 'bin.install_symlink libexec/"jarvis" => "jarvis-server"' in formula
    assert 'formula_opt_bin("universal-ctags")/"ctags"' in formula
    assert 'Formula["universal-ctags"]' not in formula
    assert 'exec "#{ctags}" "$@"' in formula
    assert "chmod 0755, libexec/" in formula
    assert "0o755" not in formula
    assert 'shell_output("#{bin}/jarvis --version")' in formula
    assert "{{ctags}}" not in formula
    assert "{{bin}}" not in formula
```

Leave the existing public-URL test in place; it remains an independent check.

- [ ] **Step 3: Run the formula tests and verify the new assertions fail**

```bash
uv run pytest tests/test_generate_homebrew_formula.py -q
```

Expected: FAIL in `test_render_contains_every_platform_url_and_checksum` because the rendered DSL still contains `version "0.11.0"`, and FAIL in `test_render_installs_both_names_and_ctags_shim` because it still uses `Formula["universal-ctags"]` and `0o755`.

- [ ] **Step 4: Apply the minimal formula-template changes**

In `scripts/generate_homebrew_formula.py`, remove the line containing this text from each platform resource:

```ruby
  version "{version}"
```

Keep every resource in this exact relative order:

```ruby
  url "{urls[platform]}"
  sha256 "{checksums[platform]}"
```

In `def install`, replace:

```ruby
    ctags = Formula["universal-ctags"].opt_bin/"ctags"
```

with:

```ruby
    ctags = formula_opt_bin("universal-ctags")/"ctags"
```

Replace:

```ruby
    chmod 0o755, libexec/"_internal/native-bin/universal-ctags"
```

with:

```ruby
    chmod 0755, libexec/"_internal/native-bin/universal-ctags"
```

Do not change checksum validation, artifact URL construction, CLI behavior, or platform names.

- [ ] **Step 5: Run formula and native workflow tests**

```bash
uv run pytest tests/test_generate_homebrew_formula.py tests/test_native_release_pipeline.py -q
uv run python scripts/check_versions.py
```

Expected: all tests PASS and the version checker prints `versions consistent`. The native workflow test still passes because Task 3 has not changed workflow assertions yet.

- [ ] **Step 6: Run a real local Homebrew audit against the regenerated formula**

```bash
VERSION=0.11.0
CHECKSUM_DIR="$(mktemp -d)"
TAP_NAME="jarvis-intelligence/jarvis-plan-audit"
for platform in darwin_arm64 darwin_amd64 linux_arm64 linux_amd64; do
  name="jarvis_${VERSION}_${platform}.tar.gz.sha256"
  curl -fsSL \
    "https://github.com/jarvis-intelligence/homebrew-jarvis/releases/download/v${VERSION}/${name}" \
    -o "${CHECKSUM_DIR}/${name}"
done
uv run python scripts/generate_homebrew_formula.py \
  --version "$VERSION" --release-tag "v${VERSION}" \
  --checksum-dir "$CHECKSUM_DIR" > /tmp/jarvis-audit.rb
brew tap-new --no-git "$TAP_NAME"
tap_dir="$(brew --repository "$TAP_NAME")"
git -C "$tap_dir" init -q
git -C "$tap_dir" config user.name "jarvis local audit"
git -C "$tap_dir" config user.email "noreply@jarvis-intelligence.dev"
mkdir -p "$tap_dir/Formula"
cp /tmp/jarvis-audit.rb "$tap_dir/Formula/jarvis.rb"
git -C "$tap_dir" add Formula/jarvis.rb
git -C "$tap_dir" commit -qm "jarvis formula audit candidate"
brew audit --formula "$TAP_NAME/jarvis"
audit_rc=$?
brew untap "$TAP_NAME"
rm -rf "$CHECKSUM_DIR" /tmp/jarvis-audit.rb
exit "$audit_rc"
```

Expected: `brew audit` exits 0. If it reports a new warning, fix the generator template, rerun Step 5, and regenerate the local tap. Do not suppress or special-case the warning.

- [ ] **Step 7: Commit the formula cleanup**

```bash
git add scripts/generate_homebrew_formula.py tests/test_generate_homebrew_formula.py
git commit -m "fix(homebrew): clean formula audit warnings"
```

Expected: exactly the generator and its test are staged and committed.

---

### Task 3: Make audit gating and pin all workflow actions

**Files:**
- Modify: `.github/workflows/publish-native.yml:122-187`
- Modify: `.github/workflows/test.yml:66-68`
- Modify: `.github/workflows/setup-smoke.yml:23-38`
- Modify: `.github/workflows/build-zoekt.yml:30-32`
- Modify: `.github/workflows/build-scip.yml:43-45`
- Test: `tests/test_native_release_pipeline.py:74-95`
- Create: `tests/test_workflow_action_pinning.py`

**Interfaces:**
- Consumes: the existing `workflow_job(name)` helper in `tests/test_native_release_pipeline.py`.
- Produces: a reusable invariant that every `uses:` ref in every workflow is a 40-character lowercase SHA, and a gating Homebrew audit assertion.

- [ ] **Step 1: Add the failing audit-gate assertion**

Append this test to `tests/test_native_release_pipeline.py`:

```python
def test_homebrew_formula_audit_is_gating():
    job = workflow_job("validate-homebrew")
    assert "- name: Audit formula" in job
    assert 'brew audit --formula "$JARVIS_TAP/jarvis"\n' in job
    assert 'brew audit --formula "$JARVIS_TAP/jarvis" || true' not in job
```

- [ ] **Step 2: Create the failing action-pinning test**

Create `tests/test_workflow_action_pinning.py` with:

```python
"""Guard against mutable major-version refs in GitHub Actions."""

from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES_ACTION = re.compile(
    r"(?m)^\s*(?:- )?uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)\s*(?:#.*)?$"
)


def test_every_github_action_is_sha_pinned() -> None:
    mutable: list[str] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        for match in USES_ACTION.finditer(workflow.read_text()):
            if re.fullmatch(r"[0-9a-f]{40}", match.group("ref")) is None:
                mutable.append(f"{workflow.name}: {match.group(0).strip()}")

    assert mutable == []
```

- [ ] **Step 3: Run the workflow tests and verify both fail**

```bash
uv run pytest \
  tests/test_native_release_pipeline.py::test_homebrew_formula_audit_is_gating \
  tests/test_workflow_action_pinning.py -q
```

Expected: the audit test FAIL because the workflow contains `|| true`; the pinning test FAIL with a nonempty list of mutable action refs.

- [ ] **Step 4: Replace every mutable workflow ref with the verified SHA**

Use this complete mapping and preserve each action's existing inputs and comments:

```text
actions/checkout@v4                       -> actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.4.0
actions/setup-node@v4                     -> actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0
actions/upload-artifact@v4                -> actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
actions/download-artifact@v4              -> actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
astral-sh/setup-uv@v5                     -> astral-sh/setup-uv@d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86 # v5.4.2
actions/setup-go@v5                       -> actions/setup-go@40f1582b2485089dde7abd97c1529aa768e1baff # v5.6.0
```

Apply it to all six workflow files listed above, including every occurrence rather than only `publish-native.yml`. `Homebrew/actions/setup-homebrew@1e34f2e2acaa766b7efacb8c26352e6abbb023f9` is already pinned and must remain unchanged.

- [ ] **Step 5: Make the Homebrew audit gate the job**

In `.github/workflows/publish-native.yml`, replace:

```yaml
      - name: Audit formula without blocking on custom-tap policy warnings
        run: brew audit --formula "$JARVIS_TAP/jarvis" || true
```

with:

```yaml
      - name: Audit formula
        run: brew audit --formula "$JARVIS_TAP/jarvis"
```

- [ ] **Step 6: Run tests, version checks, and parse every workflow**

```bash
uv run pytest \
  tests/test_generate_homebrew_formula.py \
  tests/test_native_release_pipeline.py \
  tests/test_workflow_action_pinning.py -q
uv run python scripts/check_versions.py
uv run python - <<'PY'
from pathlib import Path
import yaml

for path in sorted(Path(".github/workflows").glob("*.yml")):
    with path.open() as stream:
        yaml.safe_load(stream)
    print(path)
PY
```

Expected: all tests PASS, the checker prints `versions consistent`, and every workflow path prints after parsing successfully.

- [ ] **Step 7: Re-run the local audit from Task 2 Step 6**

Repeat Task 2 Step 6 verbatim. Expected: audit exits 0 and the temporary tap is removed.

- [ ] **Step 8: Commit workflow hardening**

```bash
git add .github/workflows/publish-native.yml \
  .github/workflows/test.yml \
  .github/workflows/setup-smoke.yml \
  .github/workflows/build-zoekt.yml \
  .github/workflows/build-scip.yml \
  tests/test_native_release_pipeline.py \
  tests/test_workflow_action_pinning.py
git commit -m "fix(ci): gate formula audit and pin actions"
```

Expected: only workflow and workflow-test files are committed.

- [ ] **Step 9: Open the source PR and merge only after CI is green**

```bash
git push -u origin fix/homebrew-audit-hardening
cat > /tmp/jarvis-homebrew-hardening-pr.md <<'PR_BODY'
## Summary
- clean Homebrew formula audit warnings
- make Homebrew formula audit gating
- SHA-pin all GitHub Actions

## Validation
- `uv run pytest tests/test_generate_homebrew_formula.py tests/test_native_release_pipeline.py tests/test_workflow_action_pinning.py -q`
- `uv run python scripts/check_versions.py`
- local `brew audit --formula jarvis-intelligence/jarvis-plan-audit/jarvis`
PR_BODY
gh pr create \
  --repo phuongddx/jarvis \
  --base main \
  --head fix/homebrew-audit-hardening \
  --title "fix(ci): harden homebrew release audits" \
  --body-file /tmp/jarvis-homebrew-hardening-pr.md
PR_NUMBER="$(gh pr view fix/homebrew-audit-hardening \
  --repo phuongddx/jarvis --json number --jq .number)"
gh pr checks "$PR_NUMBER" --repo phuongddx/jarvis --watch --fail-fast --interval 30
gh pr merge "$PR_NUMBER" --repo phuongddx/jarvis --merge
rm -f /tmp/jarvis-homebrew-hardening-pr.md
```

Expected: all checks pass, the PR merges, and no force merge or failed-check bypass occurs.

---

### Task 4: Change the plugin launcher contract to Homebrew

**Files:**
- Modify: `scripts/check-plugin.mjs:153-241` and `scripts/check-plugin.mjs:752-758` in `jarvis-index`
- Modify: `.codex-plugin/plugin.json:3,39-40` in `jarvis-index`
- Modify: `plugin/.claude-plugin/plugin.json:4` in `jarvis-index`
- Modify: `plugin/.cursor-plugin/plugin.json:4` in `jarvis-index`
- Modify: `plugin/.mcp.json` and `plugin/mcp.json` in `jarvis-index`

**Interfaces:**
- Consumes: Homebrew-installed `jarvis-server` already on `PATH`.
- Produces: byte-identical MCP configs and a `check-plugin.mjs` P2a contract that requires `command == "jarvis-server"` and no `args`.

- [ ] **Step 1: Refresh the plugin repository and create its branch**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis-index
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c fix/homebrew-plugin-launcher
git status --short
```

Expected: branch starts at `origin/main`. Existing untracked planning files may remain but must never be staged.

- [ ] **Step 2: Change the plugin checker first so the old launcher fails**

In `scripts/check-plugin.mjs`, replace the file-top P2a explanation with:

```javascript
//   P2a — the default registration launches the Homebrew-owned
//        jarvis-server command and carries no uv-managed args. The three
//        plugin.json versions still supply releaseVersion for P2b's tag
//        identity; check-manifests.mjs owns three-way version equality.
```

Also update the inline P2a banner from `manifest version ↔ jarvis-mcp floor identity` to `Homebrew launcher identity`.

Keep `PLUGIN_MANIFEST_PATHS`, `readManifestVersion`, and `releaseVersion`; they are also used by P2b. Delete `versionLessThan` and the entire old `mcpFloorRequirement` parsing block. Replace the block beginning `// P2a reads the requirement` through the closing brace before the P2b banner with:

```javascript
// P2a reads the launcher out of plugin/mcp.json alone; plugin/.mcp.json is
// its byte-identical twin, and that equality is check-manifests.mjs's
// contract, not this dimension's.
const MCP_CONFIG_PATH = 'plugin/mcp.json'
let mcpCommand = null
let parsedServer = null
try {
  const config = JSON.parse(readFileSync(resolve(process.cwd(), MCP_CONFIG_PATH), 'utf8'))
    ?.mcpServers?.jarvis
  if (typeof config !== 'object' || config === null || Array.isArray(config)) {
    fail('P2a', `${MCP_CONFIG_PATH}: mcpServers.jarvis must be an object`)
  } else {
    parsedServer = config
    mcpCommand = config.command
  }
} catch (err) {
  fail('P2a', `${MCP_CONFIG_PATH}: could not read or parse (${err.message})`)
}

if (mcpCommand !== 'jarvis-server') {
  fail('P2a', `${MCP_CONFIG_PATH}: command must be "jarvis-server" from the Homebrew install, got ${JSON.stringify(mcpCommand ?? null)}`)
}
if (mcpCommand === 'jarvis-server' && parsedServer?.args !== undefined) {
  fail('P2a', `${MCP_CONFIG_PATH}: args must be absent — Homebrew owns the launcher and its environment`)
}
```

Replace the P2a summary line near line 754 with:

```javascript
  `P2a (jarvis-server is the Homebrew-owned MCP launcher)`,
```

Do not change P2b's `expectedTag = \`v${releaseVersion}\`` derivation.

- [ ] **Step 3: Run the checker and verify the old uv launcher fails**

```bash
node scripts/check-plugin.mjs
```

Expected: FAIL with `P2a: plugin/mcp.json: command must be "jarvis-server" from the Homebrew install, got "uvx"`.

- [ ] **Step 4: Replace both MCP configs with the exact Homebrew launcher**

Write this exact content to both `plugin/.mcp.json` and `plugin/mcp.json`:

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "jarvis-server"
    }
  }
}
```

Then verify:

```bash
cmp plugin/.mcp.json plugin/mcp.json
node scripts/check-manifests.mjs
```

Expected: `cmp` exits 0 and the manifest checker prints that three manifests agree and the MCP config pair is byte-identical.

- [ ] **Step 5: Bump all three plugin manifests to `0.11.0` and pin their URLs**

In all of `.codex-plugin/plugin.json`, `plugin/.claude-plugin/plugin.json`, and `plugin/.cursor-plugin/plugin.json`, change:

```json
  "version": "0.9.1",
```

to:

```json
  "version": "0.11.0",
```

In `.codex-plugin/plugin.json`, change both occurrences of:

```text
jarvis-index/blob/v0.9.1/
```

to:

```text
jarvis-index/blob/v0.11.0/
```

Do not change the installer URL exception, which intentionally remains on `main`.

- [ ] **Step 6: Verify launcher, manifests, and local plugin surface**

```bash
node scripts/check-manifests.mjs
node scripts/check-plugin.mjs
python3 - <<'PY'
import json
from pathlib import Path

expected = {"mcpServers": {"jarvis": {"command": "jarvis-server"}}}
for name in ("plugin/.mcp.json", "plugin/mcp.json"):
    assert json.loads(Path(name).read_text()) == expected, name
for name in (
    ".codex-plugin/plugin.json",
    "plugin/.claude-plugin/plugin.json",
    "plugin/.cursor-plugin/plugin.json",
):
    assert json.loads(Path(name).read_text())["version"] == "0.11.0", name
PY
command -v jarvis-server
jarvis --version
```

Expected: both checkers exit 0, every JSON assertion passes, and the local commands resolve to the Homebrew installation with output exactly `jarvis 0.11.0`.

- [ ] **Step 7: Commit the launcher and manifest contract**

```bash
git add scripts/check-plugin.mjs \
  .codex-plugin/plugin.json \
  plugin/.claude-plugin/plugin.json \
  plugin/.cursor-plugin/plugin.json \
  plugin/.mcp.json \
  plugin/mcp.json
git commit -m "fix(plugin): launch homebrew jarvis server"
```

Expected: exactly the six contract files are committed.

---

### Task 5: Align plugin docs with Homebrew-only distribution

**Files:**
- Modify: `README.md` in `jarvis-index`
- Modify: `plugin/README.md` in `jarvis-index`
- Modify: `plugin/skills/jarvis-setup/SKILL.md` in `jarvis-index`
- Modify: `plugin/skills/jarvis-setup/references/troubleshooting.md` in `jarvis-index`
- Modify: `plugin/skills/jarvis-issues/SKILL.md` in `jarvis-index`
- Modify: `plugin/skills/jarvis-use/references/tool-roster.md` in `jarvis-index`

**Interfaces:**
- Consumes: the launcher contract from Task 4.
- Produces: user-facing instructions with no active `uvx`/PyPI jarvis installation path and accurate Homebrew-only semantic-search behavior.

- [ ] **Step 1: Rewrite setup-skill installation around Homebrew**

In `plugin/skills/jarvis-setup/SKILL.md`, set the frontmatter description to:

```text
Install and configure Homebrew jarvis, the local-first structural code intelligence with an always-on Tree-sitter syntax baseline. Use when onboarding, registering the MCP server, or indexing a repo for the first time.
```

Replace the prerequisite `uv` bullet with:

```markdown
- **Homebrew jarvis:** run `jarvis --version`. If it is missing, install it with `brew install jarvis-intelligence/jarvis/jarvis`.
```

Replace the entire `## 2. Install jarvis + external binaries` section, up to but not including `## 3. Register the MCP server`, with:

````markdown
## 2. Install jarvis and optional indexers

```bash
brew install jarvis-intelligence/jarvis/jarvis
jarvis --version
```

Expected output: `jarvis 0.11.0`. Homebrew installs the CLI, `jarvis-server`, patched `scip`, Zoekt, and `universal-ctags`; it does not require uv, pip, or PyPI.

The syntax baseline parses git-tracked files without external tools. For language-specific SCIP enrichment, install only the needed toolchain separately:

```bash
curl -fsSL https://raw.githubusercontent.com/jarvis-intelligence/jarvis-index/main/setup.sh | sh -s -- --only scip-typescript
curl -fsSL https://raw.githubusercontent.com/jarvis-intelligence/jarvis-index/main/setup.sh | sh -s -- --only scip-python
```

Use `--only scip-swift` on macOS arm64 with Xcode, `--only scip-java` when a JDK is already installed, and `--only bash-shim` when macOS Java indexing needs bash 4.4 or newer. Do not run `setup.sh` without `--only`; the Homebrew formula is the jarvis installation. `semanticSearch` dependencies are not available in this binary distribution.
````

- [ ] **Step 2: Replace obsolete setup troubleshooting rows**

In `plugin/skills/jarvis-setup/references/troubleshooting.md`, replace the first two symptom rows with:

```markdown
| `command not found: jarvis` | Install the Homebrew binary with `brew install jarvis-intelligence/jarvis/jarvis`, run `hash -r`, then verify `jarvis --version` prints `jarvis 0.11.0`. If an old `~/.local/bin/jarvis` remains, remove the obsolete uv tool with `uv tool uninstall jarvis-mcp` and open a new shell. |
| `MCP server ... connection timed out` | Verify `command -v jarvis-server` resolves under the active Homebrew prefix and `jarvis-server` is executable. Reconnect from the MCP client. The plugin launches the Homebrew binary directly and has no package cache to warm. |
```

Replace the `command not found: scip` row with:

```markdown
| `command not found: scip` / `zoekt-git-index` | These tools are normally embedded in the Homebrew keg and do not need to be on `PATH`; use `jarvis status <slug>` instead of probing PATH. For optional language-specific enrichment, install the applicable `scip-*` toolchain with the setup-skill `--only` command. |
```

Replace the ctags row with:

```markdown
| Zoekt `sym:` queries return nothing | `universal-ctags` is a Homebrew formula dependency. Run `brew list --versions universal-ctags`; if absent, run `brew install jarvis-intelligence/jarvis/jarvis` again. Reindex the affected slug after installing or changing ctags. |
```

Replace the final `typeHierarchy` row with:

```markdown
| `typeHierarchy` errors after SCIP enrichment | Verify `jarvis --version` is `0.11.0`; Homebrew bundles the patched `scip` needed for relationships. If an older `scip` earlier on `PATH` shadows it, remove or reorder that PATH entry, then run `jarvis reindex <slug> --scip`. |
```

- [ ] **Step 3: Update issue and tool-roster references**

In `plugin/skills/jarvis-issues/SKILL.md`, replace the version-report bullet with:

```markdown
- jarvis version: `jarvis --version` (expected format: `jarvis X.Y.Z`; report the complete output).
```

Replace every `setup.sh` sentence in that file's remediation guidance with: reinstall the Homebrew formula with `brew install jarvis-intelligence/jarvis/jarvis`, then reindex.

In `plugin/skills/jarvis-use/references/tool-roster.md`, replace the `typeHierarchy` sentence beginning `The bundled setup.sh installs` with:

```markdown
The Homebrew formula bundles a patched `scip` because upstream through v0.9.0 did not populate relationships; reinstall the Homebrew formula and run `jarvis reindex <slug> --scip` after upgrading it.
```

Replace the `semanticSearch` requirement sentence with:

```markdown
In the Homebrew binary distribution, semantic indexing and search are unavailable; the tool remains registered for MCP compatibility and returns a Homebrew-specific unavailability error.
```

- [ ] **Step 4: Update the plugin README**

In `plugin/README.md`, apply these exact content changes:

1. Replace the semantic bullet with:

```markdown
- `semanticSearch` — registered for MCP compatibility; unavailable in the Homebrew distribution and returns a Homebrew-specific error.
```

2. Immediately after the sentence saying the bundled config auto-registers the server, insert this exact block:

````markdown

Install the binary first:

```bash
brew install jarvis-intelligence/jarvis/jarvis
```
````

3. Replace every `setup.sh + jarvis index` flow phrase with `verify jarvis with jarvis --version`, then `jarvis index`.

4. Replace the language-indexer sentence in the Codex section with:

```markdown
Then run the `jarvis-setup` skill (or follow its steps manually): optionally install a language-specific SCIP toolchain, then `jarvis index /path/to/repo`.
```

5. Replace the whole `### Optional extras` section with:

```markdown
### Semantic search

The Homebrew binary distribution excludes the optional semantic dependencies. `semanticSearch` remains registered so the MCP tool roster stays stable, but it returns a Homebrew-specific unavailability error. Use `searchCode` and symbol navigation instead.
```

6. Replace the privacy paragraph with:

```markdown
jarvis is local-first. Installing the Homebrew formula contacts Homebrew and the public jarvis tap release; installing an optional SCIP toolchain contacts its own distribution endpoint. At query time there is no telemetry, no analytics, and no outbound call. Published indexes are opened read-only (`mode=ro&immutable=1`).
```

7. Replace the PyPI changelog link with:

```markdown
- Changelog: <https://github.com/jarvis-intelligence/jarvis-index/releases/tag/v0.11.0>
```

- [ ] **Step 5: Update the root public-distribution README**

In `README.md`, apply these exact content changes:

1. Replace the PyPI badge with:

```markdown
[![Plugin release](https://img.shields.io/github/v/tag/jarvis-intelligence/jarvis-index?label=plugin)](https://github.com/jarvis-intelligence/jarvis-index/releases/tag/v0.11.0)
```

2. Replace the semantic-search table answer with:

```markdown
| | `semanticSearch` | Registered for compatibility; unavailable in the Homebrew distribution |
```

3. Replace the entire Quick start command block with:

````markdown
```bash
# 1. Install the standalone binary and MCP server
brew install jarvis-intelligence/jarvis/jarvis

# 2. Verify
jarvis --version                    # expect: jarvis 0.11.0

# 3. Index a repo (slug defaults to the directory name)
jarvis index /path/to/your/repo

# 4. Verify
jarvis status <slug>                # expect: indexed
```

Then install the plugin below to register `jarvis-server` and install the agent skills.
````

4. Replace the sentence after the plugin table with:

```markdown
The plugin launches the Homebrew-installed `jarvis-server`. If needed, install optional language-specific SCIP tooling afterward with the setup skill's `--only` commands.
```

5. Replace the details sentence with:

```markdown
Details, Homebrew limits, and privacy notes: [`plugin/README.md`](plugin/README.md).
```

6. Replace the Requirements bullets with:

```markdown
- **macOS or Linux with Homebrew/Linuxbrew.** Windows is not supported.
- `jarvis 0.11.0` installed by `brew install jarvis-intelligence/jarvis/jarvis`.
- `java` on `PATH` only if you index Java/Kotlin repos.
```

7. In the public-surface bullet list, replace `setup.sh, fetchable by anyone` with `optional language-tooling installer, fetchable by anyone`.

8. Remove the PyPI package sentence and replace the setup.sh layout comment with `optional language-tooling installer — SYNCED from the dev repo, do not edit here`.

9. In the editing table, describe `setup.sh` as the optional language-tooling installer synced from the development repo.

10. Replace the manifest-version independence sentence with:

```markdown
value, or it reaches nobody: `plugin/.claude-plugin/plugin.json`,
`plugin/.cursor-plugin/plugin.json`, `.codex-plugin/plugin.json`. Plugin `0.11.0` aligns with the Homebrew jarvis `0.11.0` release.
```

11. Replace the changelog link with `<https://github.com/jarvis-intelligence/jarvis-index/releases/tag/v0.11.0>`.

- [ ] **Step 6: Verify no active legacy launcher remains**

```bash
node scripts/check-manifests.mjs
node scripts/check-plugin.mjs
if grep -RInE 'uvx|jarvis-mcp>=|uv tool install "jarvis-mcp|jarvis-mcp\[semantic\]|pypi\.org/project/jarvis-mcp' \
    README.md plugin .codex-plugin; then
  echo 'active legacy launcher remains' >&2
  exit 1
fi
test "$(grep -Rc 'brew install jarvis-intelligence/jarvis/jarvis' README.md plugin/README.md plugin/skills/jarvis-setup/SKILL.md | awk -F: '{sum += $2} END {print sum}')" -ge 3
python3 - <<'PY'
from pathlib import Path
text = Path('plugin/skills/jarvis-use/references/tool-roster.md').read_text()
expected = [
    'documentSymbols', 'goToDefinition', 'findReferences', 'callHierarchy',
    'typeHierarchy', 'getIndexStatus', 'indexRepo', 'searchCode',
    'semanticSearch', 'blastRadius',
]
assert all(f'### {tool}(' in text for tool in expected)
assert 'Homebrew-specific unavailability error' in text
PY
```

Expected: both Node checks exit 0; the forbidden-legacy grep finds no file; the install command appears at least three times; all 10 roster headings remain.

- [ ] **Step 7: Commit documentation alignment**

```bash
git add README.md plugin/README.md \
  plugin/skills/jarvis-setup/SKILL.md \
  plugin/skills/jarvis-setup/references/troubleshooting.md \
  plugin/skills/jarvis-issues/SKILL.md \
  plugin/skills/jarvis-use/references/tool-roster.md
git commit -m "docs(plugin): require homebrew installation"
```

Expected: only the six documentation files are committed.

---

### Task 6: Release and verify plugin `0.11.0`

**Files:**
- External mutation: remote branch, PR, signed tag `v0.11.0`, and GitHub release in `jarvis-intelligence/jarvis-index`.

**Interfaces:**
- Consumes: merged Task 4/5 commits on the plugin branch.
- Produces: tagged, release-verified plugin whose URLs pin `v0.11.0` and whose MCP configs launch Homebrew `jarvis-server`.

- [ ] **Step 1: Verify the branch has exactly the intended commits**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis-index
git status --short
git log --oneline --reverse origin/main..HEAD
```

Expected: the worktree is clean except pre-existing unrelated untracked planning files, and the log contains exactly the Task 4 launcher commit and Task 5 documentation commit.

- [ ] **Step 2: Push and create the plugin PR**

```bash
git push -u origin fix/homebrew-plugin-launcher
cat > /tmp/jarvis-plugin-0.11.0-pr.md <<'PR_BODY'
## Summary
- launch the MCP server with the Homebrew-installed `jarvis-server`
- align plugin and docs with Homebrew-only jarvis 0.11.0
- enforce the new launcher contract in plugin checks

## Validation
- `node scripts/check-manifests.mjs`
- `node scripts/check-plugin.mjs`
- local `jarvis --version` is `jarvis 0.11.0`
PR_BODY
gh pr create \
  --repo jarvis-intelligence/jarvis-index \
  --base main \
  --head fix/homebrew-plugin-launcher \
  --title "fix(plugin): launch homebrew jarvis 0.11.0" \
  --body-file /tmp/jarvis-plugin-0.11.0-pr.md
rm -f /tmp/jarvis-plugin-0.11.0-pr.md
```

Expected: a PR URL is printed.

- [ ] **Step 3: Wait for green CI, then merge**

```bash
PR_NUMBER="$(gh pr view fix/homebrew-plugin-launcher \
  --repo jarvis-intelligence/jarvis-index --json number --jq .number)"
gh pr checks "$PR_NUMBER" --repo jarvis-intelligence/jarvis-index --watch --fail-fast --interval 30
gh pr view "$PR_NUMBER" --repo jarvis-intelligence/jarvis-index \
  --json state,mergeable \
  --jq '.state == "OPEN" and .mergeable == "MERGEABLE"'
gh pr merge "$PR_NUMBER" --repo jarvis-intelligence/jarvis-index --merge
```

Expected: checks pass, mergeability is `MERGEABLE`, and the PR merges without force.

- [ ] **Step 4: Tag and publish the plugin release**

```bash
git fetch origin
git switch main
git pull --ff-only origin main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
test -z "$(git tag --list v0.11.0)"
if gh release view v0.11.0 --repo jarvis-intelligence/jarvis-index >/dev/null 2>&1; then
  echo 'v0.11.0 already exists' >&2
  exit 1
fi
TAG_TARGET_SHA="$(git rev-parse HEAD)"
git tag -s v0.11.0 -m 'v0.11.0' "$TAG_TARGET_SHA"
git tag -v v0.11.0
git push origin v0.11.0
cat > /tmp/jarvis-plugin-release-notes.md <<'RELEASE_NOTES'
## Homebrew plugin alignment

The jarvis plugin now launches the Homebrew-installed `jarvis-server` directly.

Install the binary before enabling the plugin:

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

The plugin still exposes ten MCP tools. `semanticSearch` remains registered for compatibility but is unavailable in the Homebrew binary distribution.
RELEASE_NOTES
gh release create v0.11.0 \
  --repo jarvis-intelligence/jarvis-index \
  --verify-tag \
  --title 'v0.11.0 — Homebrew plugin alignment' \
  --notes-file /tmp/jarvis-plugin-release-notes.md
rm -f /tmp/jarvis-plugin-release-notes.md
```

Expected: signing and signature verification succeed, the tag pushes, and a published non-draft release URL is printed.

- [ ] **Step 5: Run the post-tag release checker**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis-index
node scripts/check-manifests.mjs
CHECK_PLUGIN_RELEASE=1 node scripts/check-plugin.mjs --release
```

Expected: both checks exit 0. The release checker reports P2b URLs pinned at `v0.11.0` and confirms that tag exists on `origin`.

- [ ] **Step 6: Verify the installed Homebrew MCP entrypoint**

```bash
hash -r
test "$(command -v jarvis-server)" = "$(brew --prefix)/bin/jarvis-server"
test "$(readlink "$(brew --prefix)/bin/jarvis-server")" = "../libexec/jarvis"
test "$(jarvis --version)" = "jarvis 0.11.0"
test "$(brew list --versions jarvis)" = "jarvis 0.11.0"
```

Expected: every command exits 0. On an Intel Mac or Linux checkout, substitute that platform's active Homebrew prefix; the symlink and version invariants are unchanged.

- [ ] **Step 7: Verify plugin release invariants one final time**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis-index
python3 - <<'PY'
import json
from pathlib import Path

for name in ('plugin/.mcp.json', 'plugin/mcp.json'):
    config = json.loads(Path(name).read_text())
    assert config == {'mcpServers': {'jarvis': {'command': 'jarvis-server'}}}
for name in (
    '.codex-plugin/plugin.json',
    'plugin/.claude-plugin/plugin.json',
    'plugin/.cursor-plugin/plugin.json',
):
    assert json.loads(Path(name).read_text())['version'] == '0.11.0'
PY
if grep -RInE 'uvx|jarvis-mcp>=|jarvis-mcp\[semantic\]' plugin .codex-plugin README.md; then
  echo 'legacy launcher leaked into release' >&2
  exit 1
fi
```

Expected: all JSON assertions pass and the legacy-launcher grep finds no match.

---

## Failure Handling

- If token rotation cannot be completed with least privilege, stop before source/plugin execution and leave the existing secret untouched.
- If formula audit still warns, change `scripts/generate_homebrew_formula.py`; never hand-edit published formula Ruby or add `|| true`.
- If a workflow pin test fails after upstream action maintenance, resolve the exact new patch tag with `git ls-remote`, verify its 40-character SHA, update the comment, and rerun the full test set. Do not restore a mutable major ref.
- If plugin CI fails on tool-count or frontmatter checks, fix the shipped surface; do not relax the 10-tool roster or P1–P5 invariants.
- If P2b fails because `v0.11.0` is absent, stop and inspect the tag target. Never retag an existing published tag; delete an unused local tag and create it at the merged release commit only if the remote tag was never pushed.
- If Homebrew validation resolves an old uv command, remove the obsolete `uv tool install jarvis-mcp` only with explicit user approval, run `hash -r`, and recheck `command -v`.

## Verification Summary

- `JARVIS_TAP_TOKEN` timestamp changes and the old broad token is revoked; no token value appears in logs or Git.
- Formula generator tests and a real local `brew audit` pass.
- Every workflow action is SHA-pinned and every workflow YAML parses.
- `brew audit --formula` is non-masked and workflow-tested as gating.
- Source PR and plugin PR merge only after green CI.
- Both plugin MCP configs are byte-identical, parse, and contain only the bare `jarvis-server` command.
- Plugin manifests are `0.11.0`; the plugin tag and GitHub release are `v0.11.0`.
- Plugin docs expose exactly 10 tools and no active `uvx`, `jarvis-mcp`, or PyPI launcher path.
- Local Homebrew install remains exactly `jarvis 0.11.0`, and `jarvis-server` resolves to the Homebrew keg.
