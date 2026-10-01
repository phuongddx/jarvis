# Architecture

jarvis is a local-first, single-user code-intelligence MCP server: distribution
`jarvis-mcp`, import package `jarvis`, executables `jarvis` (indexing CLI) and
`jarvis-server` (stdio MCP server), plus a localhost-only dashboard.

Core flow: `jarvis index` captures Git-tracked files → Tree-sitter syntax
baseline → optional SCIP → required Zoekt → optional semantic vectors → package
graph → atomic `current` pointer swap. Readers open immutable snapshots; the
server exposes ten MCP tools over them.

## Layout

| Path | Role |
| --- | --- |
| `src/jarvis/` | Product code: indexing, MCP tools, queries, syntax/SCIP, storage, search, semantic, graph, dashboard |
| `tests/` | Flat mirrored tests (`test_<module>.py`) and `tests/fixtures/` |
| `scripts/` | Version checks, native build, release staging, smoke checks |
| `packaging/` | PyInstaller spec and launcher for native distribution |
| `.github/workflows/` | CI matrix, native binary builds, Homebrew publication |
| `docs/` | Current contributor docs; `docs/engineering/` and `docs/features/` below |
| `docs/engineering/` | Stable engineering facts: [conventions](docs/engineering/conventions.md), [infrastructure](docs/engineering/infrastructure.md), [tech stack](docs/engineering/tech-stack.md) |
| `docs/features/` | Feature-specific notes |
| `.agents/` | Agent skills, policies, hooks, and intent/spec/review templates |
| `demo/`, `diagrams/`, `plans/`, `evals/` | Demo, diagrams, historical plans/reports, eval workspaces — not runtime code |

## Pointers

- System detail: `docs/system-architecture.md`, `docs/codebase-summary.md`
- Code standards: `docs/code-standards.md`, `docs/engineering/conventions.md`
- Dashboard: `docs/dashboard.md`
- User/install docs: `README.md`; releases: `CHANGELOG.md` and `.claude/skills/jarvis-release/SKILL.md`
