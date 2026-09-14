---
name: jarvis-release
description: Cut a new jarvis release — bump the sole version source, verify tests and uv.lock, merge a release PR, sign the tag, publish its GitHub Release, and confirm the native publish-native workflow builds, validates, and publishes the Homebrew formula. Use whenever the user asks to release, ship, or publish jarvis, even if they name only part of the pipeline.
---

# Releasing jarvis

jarvis ships to end users as a Homebrew standalone formula. Development still
uses uv and a Cython wheel, but that wheel is only PyInstaller input: no wheel,
source distribution, package-manager registration, or registry descriptor is
published.

The public formula lives in `jarvis-intelligence/homebrew-jarvis`.
`.github/workflows/publish-native.yml` generates and publishes it. It requires
the source repository's `JARVIS_TAP_TOKEN` secret to have contents write access
to that tap.

## 1. Prepare the release branch

1. Ensure `main` already contains the behavior being released.
2. Choose the semantic version.
3. Set only `version = "X.Y.Z"` in `pyproject.toml`.
4. Run the unit suite:

   ```bash
   uv run pytest -m "not integration" -rs
   ```

5. Run `uv lock` so the lockfile records the new project version.
6. Replace the `Unreleased` CHANGELOG heading with `## [X.Y.Z] - YYYY-MM-DD`,
   preserving the entry's existing subsections.
7. Run `uv run python scripts/check_versions.py`.
8. Commit `CHANGELOG.md`, `pyproject.toml`, and `uv.lock` as
   `chore: release X.Y.Z`, then open the release PR.

Watch the PR checks with `gh pr checks <PR-number> --watch --interval 15`.
Do not merge a red release PR.

## 2. Tag and publish the GitHub Release

After the release PR merges:

```bash
git checkout main && git pull origin main
git tag -s vX.Y.Z -m "vX.Y.Z" <merge-commit-sha>
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z — <short summary>" --notes-file <notes>
```

The repository requires a GPG-signed tag. Verify `git tag -v vX.Y.Z` if the
push or release reports a signature problem.

Publishing the GitHub Release triggers `publish-native.yml` in the source
repository. Its release notes should tell users to run:

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

`publish-native.yml` also accepts manual dispatch. A manual run executes only
the four platform build and extracted-archive smoke jobs; it never stages tap
release assets, validates or publishes Homebrew, or publishes the formula.
Use a GitHub Release when the full publication chain is required.

## 3. Confirm the native publish pipeline

Capture and watch the run:

```bash
run_id="$(gh run list --repo phuongddx/jarvis \
  --workflow publish-native.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id" --repo phuongddx/jarvis --exit-status
gh run view "$run_id" --repo phuongddx/jarvis --json jobs \
  --jq '.jobs[] | [.name, .conclusion] | @tsv'
```

Do not report success until all of these are true:

1. All four `build` matrix jobs pass:
   `darwin_arm64`, `darwin_amd64`, `linux_amd64`, and `linux_arm64`.
2. Staging passes: all eight archive/checksum assets are uploaded to the
   public tap's `vX.Y.Z` release and the formula is rendered.
3. All four `validate-homebrew` matrix jobs pass:
   `macos-latest`, `macos-15-intel`, `ubuntu-latest`, and `ubuntu-24.04-arm`.
4. `publish-formula` commits generated `Formula/jarvis.rb` to
   `jarvis-intelligence/homebrew-jarvis`.

A failure before the formula commit can normally be investigated and the
failed workflow jobs rerun; staging is deliberately idempotent. Never edit the
generated formula manually. If a committed formula is defective, stop and get
a human release decision before publishing a patch.

## 4. Confirm the public tap and local install

Check the generated formula commit:

```bash
gh api repos/jarvis-intelligence/homebrew-jarvis/commits \
  --jq '.[] | select(.commit.message == "chore: publish jarvis X.Y.Z") | .sha'
```

Then validate the actual user path on a clean-enough machine:

```bash
brew install jarvis-intelligence/jarvis/jarvis && jarvis --version
test "$(basename "$(command -v jarvis-server)")" = jarvis-server
```

If an earlier version is installed, `brew update && brew upgrade jarvis` must
reach `X.Y.Z`; use `brew reinstall jarvis` only to recover a damaged local
installation.

## 5. Report the release

Report the release PR, tag and GitHub Release URL, `publish-native.yml` run
URL, the four build and four validation conclusions, the public tap formula
commit SHA, and the locally installed version. If any step failed, state the
last completed step exactly — do not retry silently or cut a patch without a
decision.
