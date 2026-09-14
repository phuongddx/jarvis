# jarvis — Homebrew standalone binary distribution

**Date:** 2026-09-13  
**Status:** Approved design (brainstorming session, all sections user-approved)  
**Target release:** first post-0.10 native-distribution release

## Problem

jarvis is currently an end-user Python package installed with `uv tool install
jarvis-mcp`, then optionally enriched by `setup.sh` fetching native SCIP and
Zoekt binaries. That exposes users to three concepts that jarvis itself should
own: a Python package manager, a PyPI distribution, and a separate native
bootstrap script.

The goal is one self-contained installation command that provides jarvis, its
MCP server, its Python runtime, and the native binaries needed for exact SCIP
navigation and Zoekt search. The command should work on both supported
platform families and require no Python, uv, pip, or PyPI at runtime.

## Approved direction

Use PyInstaller standalone-directory archives distributed through a custom
Homebrew tap:

```bash
brew install jarvis-intelligence/jarvis/jarvis
```

Homebrew resolves the shorthand to the public `jarvis-intelligence/homebrew-jarvis`
tap and its `jarvis` formula. A custom formula is used rather than a cask
because casks are macOS-only while jarvis also supports Linux/Homebrew-on-Linux.

There are no other end users to migrate at this point. Therefore this is a
hard cutover: once the Homebrew path works, the old end-user PyPI/uv path is
removed from documentation and automation. No migration command, transition
release, or compatibility layer is built.

## Decisions

| Decision | Choice |
|---|---|
| Distribution shape | PyInstaller one-directory standalone app, not `--onefile` |
| End-user install | Custom Homebrew formula on macOS and Linuxbrew |
| Platform matrix | macOS arm64/x86_64; Linux arm64/x86_64 |
| Embedded Python | One pinned Python 3.12 runtime |
| Native binaries bundled | Pinned `scip`, `zoekt-git-index`, `zoekt-webserver` |
| Universal ctags | Homebrew dependency, private `universal-ctags` shim |
| Optional extras | `watch` included; `semantic` excluded |
| Language indexers | Not bundled; discovered from user PATH |
| Release artifacts | Public GitHub Releases in the Homebrew tap repository |
| Old install surface | Removed after the new formula passes all platform tests |
| Migration work | None; no external users to support |

`--onefile` was rejected because every invocation unpacks its embedded runtime
and binaries to a temporary directory. That increases MCP startup latency and
creates needless disk churn. A standalone directory is still a native launcher:
users execute `jarvis`/`jarvis-server`, not Python.

## Non-goals

- No submission to Homebrew core and no promise of bare `brew install jarvis`.
- No macOS Developer ID signing, notarization, or Gatekeeper workflow in this
  first private-use release. PyInstaller's normal arm64 ad-hoc signing is
  sufficient for an unsigned local binary.
- No semantic-search distribution. LanceDB, sentence-transformers, and torch
  would make the artifacts enormous and much less reliable to build.
- No bundling of `scip-python`, `scip-typescript`, `scip-java`, or
  `scip-swift`; those rely on user toolchains such as Node, a JDK, or Xcode.
- No support work for existing uv/PyPI installations.
- No external Claude-plugin changes as part of this repository's implementation.
  The local MCP command can be changed manually after installation.
- No attempt to rewrite immutable PyPI or MCP Registry history.

## Native package layout

Each release archive contains this shape:

```text
jarvis/
  bin/jarvis                     # symlink to ../libexec/jarvis
  bin/jarvis-server              # second symlink to ../libexec/jarvis
  libexec/jarvis                 # PyInstaller native launcher
  libexec/_internal/             # Python runtime and collected dependencies
  libexec/_internal/native-bin/
    scip
    zoekt-git-index
    zoekt-webserver
```

The two public names intentionally resolve to one launcher. The launcher reads
`argv[0]`:

- basename `jarvis` → `jarvis.index_cli:main`
- basename `jarvis-server` → `jarvis.server:main`

This avoids duplicating the complete embedded Python runtime for each command.

The archive is directly runnable after extraction. Homebrew also installs the
same tree under its Cellar and links the two names from Homebrew's `bin`.

### Frozen runtime discovery

Add a small runtime module, expected to be `src/jarvis/runtime.py`, called by
the PyInstaller entrypoint before dispatching to the existing CLI/server
entrypoints.

In a normal development install it does nothing. In a frozen build it:

1. Locates `libexec/_internal/native-bin`.
2. Prepends that directory to `PATH`.
3. Sets an internal frozen-distribution marker used by capability messages and
   the semantic-install gate.

Existing subprocess code continues invoking `scip`, `zoekt-git-index`, and
`zoekt-webserver` by name. This keeps the indexing/query seams unchanged.

Precedence is:

1. `JARVIS_ZOEKT_BIN`, when explicitly set;
2. jarvis's bundled pinned binaries;
3. unrelated binaries elsewhere on `PATH`.

Bundled binaries win over arbitrary PATH copies so indexing uses the exact SCIP
fork schema and Zoekt build jarvis was tested with.

### Universal ctags

Zoekt searches for the literal executable name `universal-ctags`, while
Homebrew's formula installs the executable as `ctags`. The formula declares a
dependency on `universal-ctags` and creates a private shim named
`universal-ctags` inside jarvis's installed `native-bin` directory. No generic
`ctags` name is claimed in Homebrew's public `bin`.

### Source protection

Native packaging starts from the existing Cython-compiled wheel
(`JARVIS_COMPILE=1`), not readable source. The wheel already intentionally
keeps only `jarvis/__init__.py` and vendored `scip_pb2.py` as source files;
the package checker permits those same exceptions and rejects all other
readable jarvis implementation modules.

Third-party Python dependencies may still contain ordinary `.py` files. The
source-protection rule concerns jarvis's own implementation, matching the
current wheel policy.

## Build inputs

Packaging remains an internal use of uv and setuptools; uv is no longer an
end-user dependency.

A pinned packaging dependency group adds PyInstaller. The build creates a
clean packaging virtual environment containing:

- the platform's Cython-compiled jarvis wheel;
- the `watch` extra;
- PyInstaller;
- no `semantic` extra.

The PyInstaller spec explicitly handles:

- both jarvis entrypoints through the dispatching launcher;
- FastMCP/MCP modules discovered through imports and hidden-import rules;
- dashboard HTML/CSS/JS assets;
- tree-sitter core and every grammar provider native library;
- protobuf support around vendored `scip_pb2.py`;
- zstandard native support;
- the three pinned native binaries;
- watchdog for `jarvis watch`;
- exclusion of uv, pip, wheel-building tools, LanceDB, sentence-transformers,
  and torch.

The spec is checked in so local and CI builds use identical collection rules.

## Release pipeline

Replace `publish-pypi.yml` with `publish-native.yml`.

### Matrix

| Artifact | Builder |
|---|---|
| `jarvis_<version>_darwin_arm64.tar.gz` | `macos-latest` |
| `jarvis_<version>_darwin_amd64.tar.gz` | `macos-15-intel` |
| `jarvis_<version>_linux_arm64.tar.gz` | ARM runner in a manylinux container |
| `jarvis_<version>_linux_amd64.tar.gz` | x86_64 runner in a manylinux container |

Linux builds use a pinned manylinux environment so extension modules do not
accidentally require the newest GitHub runner glibc. macOS builds use Python
3.12 and preserve normal ad-hoc arm64 signatures.

### Per-platform steps

1. Run the normal unit suite with integration tests excluded.
2. Build the Cython-compiled jarvis wheel.
3. Build the clean PyInstaller packaging environment.
4. Download the pinned public `scip` and Zoekt release archives.
5. Verify their published checksum files.
6. Copy only the required native executables into `native-bin`.
7. Run PyInstaller.
8. Verify the generated tree before compression.
9. Create the platform tarball and SHA-256 checksum.
10. Extract that tarball into a clean location and run smoke tests against the
    extracted copy, not the original build tree.

A failed platform build fails the release. The workflow does not publish a
partial matrix.

### Native smoke suite

Every extracted artifact must pass:

- `jarvis --help` without the repository virtualenv on `PATH`;
- a real MCP JSON-RPC handshake through `jarvis-server`;
- `jarvis index` on a mini git repository;
- an exact SCIP navigation query;
- a Zoekt `searchCode` query;
- `jarvis watch` startup/import;
- a structured unavailable-capability response from `semanticSearch`.

The smoke environment proves there is no dependency on the developer `.venv`,
uv, PyPI, or a system Python interpreter.

## Homebrew tap

Create the public repository:

```text
jarvis-intelligence/homebrew-jarvis
  Formula/jarvis.rb
```

The generated formula:

- declares the jarvis version;
- selects the correct archive using Homebrew OS/CPU conditionals;
- contains the four archive SHA-256 values;
- depends on `universal-ctags`;
- installs the standalone tree under `libexec`;
- links `jarvis` and `jarvis-server`;
- creates the private `universal-ctags` shim;
- provides a `test` block that launches the CLI and checks the packaged version.

After all four artifact jobs pass, the private repository's release workflow:

1. Creates or updates a public release in the tap repository.
2. Uploads all archives and checksum files.
3. Regenerates `Formula/jarvis.rb`.
4. Commits the formula update.
5. Installs from that formula and runs Homebrew validation.
6. Marks the release successful only after validation passes.

All downloadable URLs point to the public tap repository. No formula URL points
at the private jarvis repository.

### Validation commands

```bash
brew install --formula ./Formula/jarvis.rb
brew test jarvis
brew audit --formula jarvis
```

A warning that a custom tap ships prebuilt binaries is acceptable if Homebrew
audit emits one; a broken install, checksum mismatch, missing architecture, or
failing MCP/CLI smoke test is not.

## Repository cleanup

After the Homebrew formula passes on all four platforms:

- Remove the PyPI publication workflow.
- Remove the MCP Registry publication workflow and `server.json`.
- Remove the public-distribution sync workflow that publishes the old bootstrap
  surface.
- Update `scripts/check_versions.py` and its tests for the remaining local
  version sources.
- Update the maintainer release runbook to describe native artifacts and the
  Homebrew tap rather than PyPI and the MCP Registry.
- Change the README quick start to the Homebrew command.
- Remove end-user `uv tool`, `uvx`, and PyPI instructions.
- Keep `pyproject.toml`, `uv.lock`, and Cython wheel building for development
  and as PyInstaller input.
- Keep MCP client configuration as `{ "command": "jarvis-server" }`.

The PyPI project name and packaging metadata remain useful internally even
though new versions are no longer published to PyPI.

### Bootstrap script

Reduce `setup.sh` to an optional language-indexer helper. It no longer installs:

- jarvis itself;
- `scip`;
- Zoekt;
- universal-ctags.

It may still install:

- `scip-python`;
- `scip-typescript`;
- `scip-java`;
- `scip-swift`;
- the macOS bash shim needed by `scip-java`.

This keeps the genuinely language/toolchain-specific prerequisites in one
helper while the Homebrew formula owns the common native runtime.

### Semantic behavior

The frozen distribution excludes semantic dependencies. The interactive offer
that runs `uv pip install jarvis-mcp[semantic]` is disabled whenever the frozen
marker is set. It cannot modify a PyInstaller application and would incorrectly
make uv part of the user-facing runtime.

`semanticSearch` continues to return a structured capability error. Existing
Zoekt and symbol-search behavior remains available.

### Recovery messages

Messages that currently tell users to rerun `setup.sh` for SCIP/Zoekt are
updated:

- missing/corrupt embedded runtime → `brew reinstall jarvis`;
- missing optional language indexer → install that specific language toolchain;
- unsupported architecture → Homebrew refuses before installation;
- checksum mismatch → Homebrew refuses the archive.

## Verification

### Unit tests

- Development runtime initialization is a no-op.
- Frozen initialization resolves `native-bin` and prepends it to `PATH`.
- `JARVIS_ZOEKT_BIN` overrides the bundled webserver path.
- Bundled binaries precede unrelated PATH entries.
- Launcher dispatch selects the CLI/server according to `argv[0]`.
- Frozen mode suppresses the uv semantic-extra offer.
- `semanticSearch` remains a structured unavailable response in frozen mode.
- Version checks reflect the removal of `server.json`.

### Package checker

Add a checker that rejects a native archive when it:

- contains readable jarvis implementation modules other than the two wheel
  exceptions (`__init__.py`, vendored `scip_pb2.py`);
- includes `lancedb`, `sentence_transformers`, or `torch`;
- includes uv, pip bootstrap machinery, or wheel-building tools as user-facing
  runtime components;
- omits dashboard assets;
- omits the tree-sitter runtime or any required grammar provider;
- omits any required native binary;
- marks a native binary non-executable;
- mixes architectures;
- omits either public launcher name.

### Homebrew tests

- Formula installation succeeds on each supported platform/architecture.
- `jarvis` and `jarvis-server` are both linked.
- The launcher reports the packaged jarvis version.
- The MCP stdio server completes initialization and responds to JSON-RPC.
- SCIP and Zoekt subprocesses resolve inside the Homebrew Cellar tree.
- `brew reinstall jarvis` repairs a deliberately removed embedded binary.

## Operational prerequisites

Before the first release:

1. Create `jarvis-intelligence/homebrew-jarvis`.
2. Grant the private repository's workflow permission to create releases and
   update formula contents in that public repository.
3. Seed the tap with an initial formula-only commit if needed by credential
   setup; production artifacts still come only from the release workflow.
4. Run one manual workflow dispatch and verify all four platform artifacts
   before deleting the old publication workflows.

## Success criteria

- The one-line Homebrew command installs a working standalone distribution on
  macOS arm64/x86_64 and Linux arm64/x86_64.
- Both `jarvis` and `jarvis-server` run without Python, uv, pip, or PyPI.
- Exact SCIP navigation, Zoekt search, dashboard assets, and `watch` work.
- `semanticSearch` fails clearly and never asks the user to install uv.
- No new release is published to PyPI or the MCP Registry.
- The four-artifact native release pipeline gates formula publication on every
  platform passing.
