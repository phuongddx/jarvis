# Homebrew Tap Release Enablement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status: EXECUTED — historical record.** This plan shipped `v0.11.0` on 2026-09-14. Do not execute its release, tag, tap-creation, or token steps again. The unchecked boxes are a historical artifact of the ledger workflow, not pending work. Read the post-execution audit below with the original steps.

**Goal:** Create the missing public Homebrew tap, configure its scoped publish credential, release jarvis `0.11.0` through `publish-native.yml`, and verify `brew install jarvis-intelligence/jarvis/jarvis`.

**Architecture:** Use the already-merged native release pipeline unchanged. Create and seed `jarvis-intelligence/homebrew-jarvis`, provide a repository-scoped token, validate the four native builds by manual dispatch, then publish a GPG-signed `v0.11.0` release. The existing release workflow uploads eight checksummed artifacts, renders the formula, validates real Homebrew installs on four runners, and commits the generated formula.

**Tech Stack:** GitHub CLI, GitHub Actions, fine-grained GitHub PAT, Homebrew, GPG-signed Git tags, uv, Python 3.12.

**Spec:** [`docs/superpowers/specs/2026-09-13-homebrew-standalone-binary-distribution-design.md`](../specs/2026-09-13-homebrew-standalone-binary-distribution-design.md) § Operational prerequisites, plus [`.claude/skills/jarvis-release/SKILL.md`](../../../.claude/skills/jarvis-release/SKILL.md).

## Scope Check

This is one operational release-enablement plan, not a packaging redesign. The native packaging code is already merged. This plan only supplies its missing external tap/credential prerequisites and ships `0.11.0`.

## Post-Execution Audit — 2026-09-15

### Execution result

- Source tag and release: `v0.11.0`.
- Source release commit: `fefc47f` (`fefc47fb9d945e42478ee703f45bf4ea36b5b80e`).
- Successful release workflow: `34878937825`.
- Public tap: `jarvis-intelligence/homebrew-jarvis`.
- Formula-publishing tap commit: `0e961899` (`0e961899576435cb3d52608ad1c9e08c645792ac`).
- Local Homebrew installation: `jarvis 0.11.0`; `brew test jarvis` passes.
- Both `jarvis` and `jarvis-server` resolve to the active Homebrew prefix after the legacy uv `jarvis-mcp` tool was removed with explicit user approval.

### Corrections discovered during execution

- The original statement that the release workflow was unchanged was false. Execution required hardening PRs #57–#63:
  - #57 stabilized native release validation, including host-python smoke execution and retry behavior.
  - #58 prepared `0.11.0`.
  - #59 fixed release-asset globs and prevented the staged `jarvis/` directory from being uploaded.
  - #60 pinned `Homebrew/actions/setup-homebrew`.
  - #61 moved Homebrew validation to a deterministic local tap.
  - #62 initialized the local validation tap with a safe Git identity.
  - #63 fixed the stripped formula install/layout.
- The Linux post-build smoke now uses the runner's host `python3`, not the removed packaged interpreter.
- Native archive downloads retry transient HTTP and network failures.
- Artifact and release-upload globs are `jarvis_*.tar.gz*`, not all release-directory files.
- The correct current Homebrew CLI is `brew test jarvis`; `brew test --formula jarvis` is invalid on Homebrew 7.0.1. The later `brew test "$JARVIS_TAP/jarvis"` form used by the workflow is also correct.
- The old artifact probe allowed a jq result of `false` to pass because it checked command exit status rather than the boolean value. Current checks must compare the jq result explicitly to `true`.
- The original plan did not include removal of the obsolete uv `jarvis-mcp` tool or alignment of the separately versioned Codex/Claude/Cursor plugin.
- During release, the user explicitly directed use of the existing broad GitHub CLI token for `JARVIS_TAP_TOKEN`. This succeeded but is not the desired final state: replace it with a fine-grained PAT scoped only to `jarvis-intelligence/homebrew-jarvis`, with Contents read/write and Metadata read. Do not print or persist the token.
- The formula currently passes functional validation but still produces audit warnings for redundant `version`, DSL ordering, `Formula["universal-ctags"]`, and `0o755`. The release workflow currently masks `brew audit` with `|| true`.

### Follow-up

The successor plan is [`2026-09-15-homebrew-release-hardening-and-plugin-alignment.md`](2026-09-15-homebrew-release-hardening-and-plugin-alignment.md).

## Global Constraints

- Tap repository is exactly `jarvis-intelligence/homebrew-jarvis`.
- The tap repository is public.
- The token secret name is exactly `JARVIS_TAP_TOKEN` in `phuongddx/jarvis`.
- The token is repository-scoped to only `jarvis-intelligence/homebrew-jarvis`; grant only Contents: Read and write plus Metadata: Read.
- Never print, commit, or store the token value.
- Release version is exactly `0.11.0`; tag is exactly `v0.11.0`.
- `v0.11.0` must not already exist locally or remotely.
- Tag `v0.11.0` is a GPG-signed annotated tag pushed only after its release PR is green and merged.
- Manual `publish-native.yml` dispatch builds and smoke-tests four archives but never publishes tap assets or a formula.
- Release publication requires all four `build` jobs, `stage-release`, all four `validate-homebrew` jobs, and `publish-formula` to succeed.
- Never hand-edit `Formula/jarvis.rb`; it is generated by `publish-native.yml`.
- Final local install command is exactly `brew install jarvis-intelligence/jarvis/jarvis`.
- Do not run this plan from an unmerged feature branch.

---

### Task 1: Refresh `main` and verify the release base

**Files:**
- No files changed.

**Interfaces:**
- Produces: local `main` equal to `origin/main` and containing commit `81b0ac6`.
- Confirms: `v0.11.0` is unused.

- [ ] **Step 1: Confirm a clean worktree and refresh `main`**

```bash
cd /Users/ddphuong/Projects/jarvis-ai/jarvis
git status --short
git fetch origin
git switch main
git pull --ff-only origin main
```

Expected: only pre-existing unrelated untracked files remain, and the pull is a fast-forward to or beyond `250ca7f`.

- [ ] **Step 2: Verify merged native workflow and CI fix are present**

```bash
test -f .github/workflows/publish-native.yml
git merge-base --is-ancestor 81b0ac6 origin/main
grep -n 'workflow_dispatch:' .github/workflows/publish-native.yml
grep -n 'JARVIS_TAP_TOKEN' .github/workflows/publish-native.yml
```

Expected: all commands exit 0.

- [ ] **Step 3: Verify `0.11.0` is unused**

```bash
test "$(python3 - <<'PY'
import tomllib
print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])
PY
)" = 0.10.0
test -z "$(git tag --list v0.11.0)"
if gh release view v0.11.0 --repo phuongddx/jarvis >/dev/null 2>&1; then
  echo 'v0.11.0 already exists' >&2
  exit 1
fi
```

Expected: current packaged version is `0.10.0`, and `v0.11.0` is unused.

- [ ] **Step 4: Run the merged-main test gate**

```bash
uv sync --extra semantic --group native
uv run pytest -m "not integration" -rs
uv run python scripts/check_versions.py
```

Expected: pytest exits 0; version checker prints `versions consistent`.

- [ ] **Step 5: Record the release base**

```bash
git rev-parse origin/main
```

Record this SHA as the release base for audit.

---

### Task 2: Create and seed the public tap

**Files:**
- Create in external repo: `README.md`
- Create in external repo: `Formula/.gitkeep`

**Interfaces:**
- Produces: public repository `jarvis-intelligence/homebrew-jarvis` with default branch `main`.
- Task 7 writes `Formula/jarvis.rb` into this repository.

- [ ] **Step 1: Verify the tap is absent before creating it**

```bash
TAP_REPO=jarvis-intelligence/homebrew-jarvis
if gh repo view "$TAP_REPO" --json name,owner,visibility,url >/dev/null 2>&1; then
  echo "$TAP_REPO already exists; stop and review it before continuing" >&2
  exit 1
fi
```

Expected: command exits nonzero because the repository is absent.

- [ ] **Step 2: Create the public organization repository**

```bash
TAP_REPO=jarvis-intelligence/homebrew-jarvis
gh repo create "$TAP_REPO" \
  --public \
  --description "Homebrew formula for jarvis"
```

Expected: GitHub CLI prints the repository URL.

- [ ] **Step 3: Clone and seed the tap**

````bash
TAP_REPO=jarvis-intelligence/homebrew-jarvis
TAP_TMP="$(mktemp -d)"
git clone "https://github.com/${TAP_REPO}.git" "$TAP_TMP"
cd "$TAP_TMP"
mkdir -p Formula
cat > README.md <<'TAP_README'
# jarvis Homebrew tap

This tap publishes generated jarvis standalone-distribution formulae.

Install the current release with:

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

`Formula/jarvis.rb` is generated by the jarvis release workflow. Do not edit it by hand.
TAP_README
touch Formula/.gitkeep
git add README.md Formula/.gitkeep
git commit -m "chore: initialize jarvis Homebrew tap"
git push origin main
````

Expected: push succeeds and the default branch is `main`.

- [ ] **Step 4: Verify public visibility and seeded layout**

```bash
cd - >/dev/null
TAP_REPO=jarvis-intelligence/homebrew-jarvis
curl -sS -o /dev/null -w '%{http_code}\n' "https://github.com/${TAP_REPO}"
gh repo view "$TAP_REPO" --json visibility,defaultBranchRef \
  --jq '.visibility == "PUBLIC" and .defaultBranchRef.name == "main"'
gh api "repos/${TAP_REPO}/contents/Formula" \
  --jq 'map(.name) | contains([".gitkeep"])'
```

Expected: HTTP `200`, both jq checks print `true`, and the API lists `.gitkeep`.

---

### Task 3: Configure the scoped tap publish token

**Files:**
- No repository files changed.

**Interfaces:**
- Produces: Actions secret `JARVIS_TAP_TOKEN` in `phuongddx/jarvis`.
- `publish-native.yml` reads it at `stage-release` and `publish-formula`.

- [ ] **Step 1: Create a fine-grained PAT in GitHub**

In GitHub, while authenticated as `phuongddx`:

1. Open **Settings → Developer settings → Fine-grained tokens → Generate new token**.
2. Set token name to `jarvis-homebrew-tap-release`.
3. Set expiration to at least `7 days`.
4. Set resource owner to `jarvis-intelligence`.
5. Set repository access to **Only select repositories**, then select `jarvis-intelligence/homebrew-jarvis`.
6. Under **Repository permissions**, set:
   - **Contents:** Read and write
   - **Metadata:** Read-only
7. Generate and copy the token.

Do not store the token in a file, command history, gist, or repository.

- [ ] **Step 2: Set the Actions secret without exposing the value**

Run this as one interactive shell sequence:

```bash
read -rs JARVIS_TAP_TOKEN_VALUE
test -n "$JARVIS_TAP_TOKEN_VALUE"
printf '%s' "$JARVIS_TAP_TOKEN_VALUE" | gh secret set JARVIS_TAP_TOKEN \
  --repo phuongddx/jarvis
unset JARVIS_TAP_TOKEN_VALUE
gh secret list --repo phuongddx/jarvis
```

Expected: `gh secret set` exits 0 and `gh secret list` includes `JARVIS_TAP_TOKEN`.

- [ ] **Step 3: Record only non-secret metadata**

```bash
gh secret list --repo phuongddx/jarvis | grep '^JARVIS_TAP_TOKEN'
```

Expected: exactly one matching secret name is listed. Do not attempt to print or verify its value.

---

### Task 4: Run the manual four-platform build validation

**Files:**
- No files changed.

**Interfaces:**
- Consumes: merged `publish-native.yml` on `main`.
- Produces: four successful `native-*` workflow artifacts and confidence that Linux builds pass before release.
- Does not create tap assets or formula.

- [ ] **Step 1: Capture the dispatch base**

```bash
git fetch origin main
DISPATCH_SHA="$(git rev-parse origin/main)"
git merge-base --is-ancestor 81b0ac6 "$DISPATCH_SHA"
gh workflow run publish-native.yml --ref main
sleep 5
```

Expected: dispatch succeeds and `origin/main` is unchanged.

- [ ] **Step 2: Find the manual run**

```bash
DISPATCH_SHA="$(git rev-parse origin/main)"
RUN_ID="$(gh run list \
  --workflow publish-native.yml \
  --repo phuongddx/jarvis \
  --limit 20 \
  --json databaseId,event,headBranch,headSha,createdAt \
  --jq "[.[] | select(.event == \"workflow_dispatch\" and .headBranch == \"main\" and .headSha == \"${DISPATCH_SHA}\")] | sort_by(.createdAt) | last | .databaseId")"
test -n "$RUN_ID"
printf 'manual native run: %s\n' "$RUN_ID"
```

Expected: one nonempty run ID.

- [ ] **Step 3: Wait for the manual run**

```bash
gh run watch "$RUN_ID" --repo phuongddx/jarvis --exit-status --interval 30
```

Expected: run conclusion is `success`.

- [ ] **Step 4: Verify all four build jobs succeeded**

```bash
gh run view "$RUN_ID" --repo phuongddx/jarvis --json conclusion,jobs --jq '
  .conclusion == "success"
  and ([.jobs[] | select(.name | startswith("build ("))] | length == 4)
  and ([.jobs[] | select(.name | startswith("build (")) | .conclusion] | all(. == "success"))
'
```

Expected: jq prints `true`.

- [ ] **Step 5: Verify manual mode created build artifacts but no formula**

```bash
gh api "repos/phuongddx/jarvis/actions/runs/${RUN_ID}/artifacts" \
  --jq '[.artifacts[].name] | sort'
if gh api "repos/phuongddx/jarvis/actions/runs/${RUN_ID}/artifacts" \
  --jq '[.artifacts[].name] | any(. == "homebrew-formula")'; then
  echo 'manual dispatch unexpectedly produced formula' >&2
  exit 1
fi
if gh release view v0.11.0 --repo jarvis-intelligence/homebrew-jarvis >/dev/null 2>&1; then
  echo 'manual dispatch unexpectedly created tap release' >&2
  exit 1
fi
```

Expected: exactly four names matching `native-*`, no `homebrew-formula`, and no `v0.11.0` tap release.

---

### Task 5: Prepare the `0.11.0` release PR

**Files:**
- Modify: `pyproject.toml:16`
- Modify: `uv.lock` through uv
- Modify: `CHANGELOG.md:3`

**Interfaces:**
- Produces: remote release branch `release/homebrew-0.11.0`.
- Produces: merged `main` with `project.version = "0.11.0"`.

- [ ] **Step 1: Create the release branch**

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c release/homebrew-0.11.0
```

Expected: branch starts at `origin/main`.

- [ ] **Step 2: Bump the package version**

```bash
uv version 0.11.0
uv lock
```

Expected: `pyproject.toml` and `uv.lock` both change; `uv version --short` prints `0.11.0`.

- [ ] **Step 3: Promote the Unreleased changelog**

```bash
python3 - <<'PY'
from pathlib import Path
path = Path('CHANGELOG.md')
text = path.read_text()
needle = '## [Unreleased]'
if text.count(needle) != 1:
    raise SystemExit('expected exactly one [Unreleased] heading')
path.write_text(text.replace(needle, '## [0.11.0] - 2026-09-14', 1))
PY
```

Expected: the existing Homebrew distribution notes move under `## [0.11.0] - 2026-09-14`.

- [ ] **Step 4: Run release checks**

```bash
uv lock --check
uv run python scripts/check_versions.py
uv run pytest -m "not integration" -rs
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit and push**

```bash
git add pyproject.toml uv.lock CHANGELOG.md
git diff --cached --check
git commit -m "chore: release 0.11.0"
git push -u origin release/homebrew-0.11.0
```

Expected: push succeeds.

- [ ] **Step 6: Open the release PR**

```bash
cat > /tmp/jarvis-0.11.0-pr-body.md <<'PR_BODY'
## Summary
- Bump the native release version to 0.11.0
- Promote the Homebrew standalone-distribution changelog entry

## Validation
- `uv lock --check`
- `uv run python scripts/check_versions.py`
- `uv run pytest -m "not integration" -rs`
PR_BODY
gh pr create \
  --base main \
  --head release/homebrew-0.11.0 \
  --title "chore: release 0.11.0" \
  --body-file /tmp/jarvis-0.11.0-pr-body.md
```

Expected: GitHub prints a pull-request URL.

- [ ] **Step 7: Wait for green CI, then merge**

```bash
PR_NUMBER="$(gh pr view release/homebrew-0.11.0 --repo phuongddx/jarvis --json number --jq .number)"
gh pr checks "$PR_NUMBER" --repo phuongddx/jarvis --watch --fail-fast --interval 30
gh pr view "$PR_NUMBER" --repo phuongddx/jarvis --json state,mergeable --jq '.state == "OPEN" and .mergeable == "MERGEABLE"'
gh pr merge "$PR_NUMBER" --repo phuongddx/jarvis --merge
```

Expected: checks pass, mergeability is `MERGEABLE`, and the PR merges. If any check fails, stop; do not force merge.

---

### Task 6: Create the signed `v0.11.0` release

**Files:**
- No tracked repository files changed.
- Creates remote tag `v0.11.0` and GitHub Release `v0.11.0`.

**Interfaces:**
- Consumes: merged `main` from Task 5.
- Triggers: full `publish-native.yml` release pipeline.

- [ ] **Step 1: Refresh merged `main`**

```bash
git fetch origin
git switch main
git pull --ff-only origin main
```

Expected: local `main` includes the release-prep PR merge.

- [ ] **Step 2: Verify release invariants**

```bash
test "$(uv version --short)" = 0.11.0
grep -q '^## \[0.11.0\] - 2026-09-14' CHANGELOG.md
git merge-base --is-ancestor 81b0ac6 HEAD
test -z "$(git tag --list v0.11.0)"
if gh release view v0.11.0 --repo phuongddx/jarvis >/dev/null 2>&1; then
  echo 'v0.11.0 already exists' >&2
  exit 1
fi
```

Expected: every command exits 0.

- [ ] **Step 3: Create and verify the signed annotated tag**

```bash
TAG_TARGET_SHA="$(git rev-parse HEAD)"
git tag -s v0.11.0 -m 'v0.11.0' "$TAG_TARGET_SHA"
git tag -v v0.11.0
git push origin v0.11.0
```

Expected: signing succeeds, signature verification succeeds, and tag push succeeds. If GPG signing fails, stop and configure the release signing key; do not use an unsigned tag.

- [ ] **Step 4: Create the GitHub release**

````bash
RELEASE_NOTES="$(mktemp /tmp/jarvis-v0.11.0-notes.XXXXXX.md)"
cat > "$RELEASE_NOTES" <<'RELEASE_NOTES'
## Homebrew standalone distribution

jarvis `0.11.0` is the first release distributed as a standalone Homebrew binary.

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

The formula bundles Python 3.12, the dashboard, Tree-sitter grammars, `watch`, pinned SCIP, Zoekt indexing, and Zoekt search. Homebrew supplies `universal-ctags`. Optional language indexers remain separate toolchain-specific installs.

## Changed

- Replaced the uv/PyPI installation path with Homebrew standalone distribution for macOS arm64/x86_64 and Linux arm64/x86_64.
- Excluded optional semantic dependencies from the standalone distribution.
- Removed PyPI, MCP Registry, and legacy bootstrap publication workflows.
RELEASE_NOTES
gh release create v0.11.0 \
  --repo phuongddx/jarvis \
  --verify-tag \
  --title "v0.11.0 — Homebrew standalone distribution" \
  --notes-file "$RELEASE_NOTES"
rm -f "$RELEASE_NOTES"
````

Expected: release URL is printed and the release is marked published.

- [ ] **Step 5: Verify the trigger**

```bash
test "$(gh release view v0.11.0 --repo phuongddx/jarvis --json isDraft --jq .isDraft)" = false
gh run list --workflow publish-native.yml --repo phuongddx/jarvis \
  --json event,headBranch,status \
  --jq '[.[] | select(.event == "release" and .headBranch == "v0.11.0")] | length > 0'
```

Expected: release is published and at least one native release run exists.

---

### Task 7: Monitor and verify the full native release pipeline

**Files:**
- No files changed in this repository.
- External result: eight release assets and committed `Formula/jarvis.rb`.

**Interfaces:**
- Consumes: `v0.11.0`, Task 2 tap, Task 3 token, and merged workflow.
- Produces: public installable formula.

- [ ] **Step 1: Find the release run**

```bash
RUN_ID="$(gh run list \
  --workflow publish-native.yml \
  --repo phuongddx/jarvis \
  --limit 30 \
  --json databaseId,event,headBranch,createdAt \
  --jq '[.[] | select(.event == "release" and .headBranch == "v0.11.0")] | sort_by(.createdAt) | last | .databaseId')"
test -n "$RUN_ID"
printf 'release native run: %s\n' "$RUN_ID"
```

Expected: one nonempty release run ID.

- [ ] **Step 2: Watch the full pipeline**

```bash
gh run watch "$RUN_ID" --repo phuongddx/jarvis --exit-status --interval 30
```

Expected: run conclusion is `success`.

- [ ] **Step 3: Verify every release job succeeded**

```bash
gh run view "$RUN_ID" --repo phuongddx/jarvis --json conclusion,jobs --jq '
  .conclusion == "success"
  and ([.jobs[] | select(.name | startswith("build ("))] | length == 4)
  and ([.jobs[] | select(.name | startswith("build (")) | .conclusion] | all(. == "success"))
  and ([.jobs[] | select(.name | startswith("validate-homebrew ("))] | length == 4)
  and ([.jobs[] | select(.name | startswith("validate-homebrew (")) | .conclusion] | all(. == "success"))
  and ([.jobs[] | select(.name == "stage-release" or .name == "publish-formula") | .conclusion] | all(. == "success"))
'
```

Expected: jq prints `true`.

- [ ] **Step 4: Verify the public release assets**

```bash
gh release view v0.11.0 --repo jarvis-intelligence/homebrew-jarvis \
  --json assets \
  --jq '[.assets[].name] | sort == [
    "jarvis_0.11.0_darwin_amd64.tar.gz",
    "jarvis_0.11.0_darwin_amd64.tar.gz.sha256",
    "jarvis_0.11.0_darwin_arm64.tar.gz",
    "jarvis_0.11.0_darwin_arm64.tar.gz.sha256",
    "jarvis_0.11.0_linux_amd64.tar.gz",
    "jarvis_0.11.0_linux_amd64.tar.gz.sha256",
    "jarvis_0.11.0_linux_arm64.tar.gz",
    "jarvis_0.11.0_linux_arm64.tar.gz.sha256"
  ]'
```

Expected: jq prints `true`.

- [ ] **Step 5: Verify the generated formula commit**

```bash
TAP_TMP="$(mktemp -d)"
git clone https://github.com/jarvis-intelligence/homebrew-jarvis.git "$TAP_TMP"
cd "$TAP_TMP"
test -f Formula/jarvis.rb
grep -q 'version "0.11.0"' Formula/jarvis.rb
grep -q 'jarvis_0.11.0_darwin_arm64.tar.gz' Formula/jarvis.rb
grep -q 'jarvis_0.11.0_linux_amd64.tar.gz' Formula/jarvis.rb
git log -1 --format=%s | grep -q 'publish jarvis 0.11.0'
cd - >/dev/null
rm -rf "$TAP_TMP"
```

Expected: every command exits 0.

---

### Task 8: Verify the user-facing Homebrew installation

**Files:**
- No repository files changed.

**Interfaces:**
- Consumes: published `jarvis-intelligence/homebrew-jarvis` formula.
- Produces: verified local install.

- [ ] **Step 1: Clear the failed tap attempt**

```bash
if brew tap | grep -q '^jarvis-intelligence/jarvis$'; then
  brew untap jarvis-intelligence/jarvis
fi
```

Expected: command exits 0 whether or not the old partial tap exists.

- [ ] **Step 2: Install through the requested command**

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

Expected: Homebrew taps the public repository, downloads the matching archive, verifies its checksum, installs jarvis, and exits 0.

- [ ] **Step 3: Verify installed layout and version**

```bash
PREFIX="$(brew --prefix jarvis)"
test -x "$PREFIX/bin/jarvis"
test -x "$PREFIX/bin/jarvis-server"
test "$(readlink "$PREFIX/bin/jarvis")" = "../libexec/jarvis"
test "$(readlink "$PREFIX/bin/jarvis-server")" = "../libexec/jarvis"
test -x "$PREFIX/libexec/_internal/native-bin/scip"
test -x "$PREFIX/libexec/_internal/native-bin/zoekt-git-index"
test -x "$PREFIX/libexec/_internal/native-bin/zoekt-webserver"
test -x "$PREFIX/libexec/_internal/native-bin/universal-ctags"
"$PREFIX/bin/jarvis" --version
```

Expected: all tests pass and the output is exactly `jarvis 0.11.0`.

- [ ] **Step 4: Run Homebrew's own formula test**

```bash
brew test --formula jarvis
```

Expected: exit 0.

- [ ] **Step 5: Verify reinstall repairs embedded binaries**

```bash
PREFIX="$(brew --prefix jarvis)"
rm "$PREFIX/libexec/_internal/native-bin/scip"
test ! -e "$PREFIX/libexec/_internal/native-bin/scip"
brew reinstall --formula jarvis
test -x "$PREFIX/libexec/_internal/native-bin/scip"
```

Expected: reinstall exits 0 and restores `scip`.

- [ ] **Step 6: Verify command resolution**

```bash
hash -r
printf 'jarvis: %s\n' "$(command -v jarvis)"
printf 'jarvis-server: %s\n' "$(command -v jarvis-server)"
```

Expected: both commands resolve under the active Homebrew prefix. If an old uv/PyPI install shadows them, remove that old tool installation or adjust PATH before considering this task complete.

---

### Failure Handling

- If a workflow fails, inspect its failed job with `gh run view --log-failed`.
- Rerun only failed jobs after identifying the cause: `gh run rerun <run-id> --failed`.
- If the same job fails twice, stop and fix on a normal PR; do not retry indefinitely.
- Never manually edit `Formula/jarvis.rb`; regenerate through the release workflow.
- If release staging fails, fix and rerun the failed jobs; `gh release upload --clobber` makes staging idempotent.
- If Homebrew validation fails, do not remove or rewrite the public release; diagnose and publish a follow-up commit/release if needed.
- If the token lacks permission, revoke it and issue a new repository-scoped fine-grained token; do not broaden an existing token.

## Verification Summary

- Public tap: `jarvis-intelligence/homebrew-jarvis` exists, is public, and has `Formula/jarvis.rb`.
- Manual validation: four native builds and extracted smoke tests pass without publishing.
- Release PR: version `0.11.0`, refreshed lockfile, promoted changelog, green CI, merged.
- Release: signed `v0.11.0` tag and published GitHub release.
- Native pipeline: all build, staging, Homebrew validation, and formula-publication jobs pass.
- Tap release: eight expected checksummed assets exist.
- Formula: generated, committed, versioned `0.11.0`, and references all four archives.
- Local install: `brew install jarvis-intelligence/jarvis/jarvis` produces `jarvis 0.11.0`; `brew test` and reinstall repair pass.
