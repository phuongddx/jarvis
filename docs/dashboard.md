# jarvis dashboard

`jarvis dashboard` serves a localhost web console over the same `~/.jarvis`
data the CLI writes and the MCP server reads: registry state, live index
logs, the query seams, and the operator actions. It blocks in the foreground
until Ctrl-C.

## Launch

```bash
jarvis dashboard              # http://127.0.0.1:6080, opens a browser
jarvis dashboard --port 7100  # different port
jarvis dashboard --no-open    # do not auto-open the browser
```

The port resolves in order: `--port` → `JARVIS_DASHBOARD_PORT` → `6080`. An
invalid or out-of-range `JARVIS_DASHBOARD_PORT` is not an error — jarvis
warns on stderr and uses 6080, so a stale shell export can never stop the
dashboard from starting.

## Views

- **Repos** — every registered repo with language, status (`indexing` /
  `indexed` / `partial` / `degraded` / `failed`), freshness, and storage
  totals. A live log tail streams the running index's output. **Index**,
  **reindex**, and **forget** act from here; forget requires typing the
  repo's exact slug before it will run — the same irreversible teardown as
  `jarvis forget`, so a mis-click cannot destroy an index.
- **Repo detail** — one repo's published snapshots (name, size, which one the
  `current` pointer selects), per-tool capabilities, recovery guidance for
  degraded/failed runs, its package-graph edges (depends on / depended on
  by), and per-store storage sizes.
- **Search** — one query fanned out to Zoekt lexical hits and SCIP symbol
  matches, scoped to one repo or across all. Source builds with semantic data
  also include vector hits; the Homebrew package excludes semantic
  dependencies. Any hit opens in the in-browser source viewer.
- **Playground** — the ten MCP tools with parameter forms generated from
  their signatures; invoke any tool and inspect the raw JSON response and
  its latency. The fastest way to see exactly what an agent sees.

## Actions

**Index / reindex** spawn the same detached `jarvis index` children as the
`indexRepo` MCP tool — same write-once launch records, same per-slug build
lock — and return immediately; a run already in flight for a slug is
rejected (HTTP 409) rather than queued. Watch the Repos view's log tail and
status row to follow the run. **Forget** shares `jarvis forget`'s teardown
code and its build lock: it refuses while a writer is live, and the typed
slug is the confirmation.

## Security model

- **Loopback only.** The server binds `127.0.0.1` — unreachable from the
  network by construction.
- **Host guard.** Requests whose `Host` header is not `127.0.0.1` or
  `localhost` are rejected with 403, which blocks DNS-rebinding; POSTs must
  carry a localhost `Origin` when one is present; an unparseable origin
  fails closed.
- **Path confinement.** The source viewer serves only files inside the
  repo's own working tree: absolute paths and `..` segments are rejected,
  and the resolved target must stay under the repo root.
- **No auth, on purpose.** Single-tenant, the same contract as the MCP
  server — the console can act on your machine's indexes, so treat the port
  as trusted-loopback surface, not a shared service.

## Environment variables

- `JARVIS_DASHBOARD_PORT` — default port when `--port` is absent (default
  `6080`; invalid values degrade to the default with a stderr warning, the
  same contract as `JARVIS_ZOEKT_PORT`).

## Troubleshooting

- **Port already in use** — `jarvis dashboard` exits with `error: ...` and
  status 1. A second dashboard instance is the usual culprit; otherwise pick
  another port with `--port N`.
- **Assets missing after a standalone install** (blank page, or 404 on
  `app.js`) — dashboard assets are bundled in the archive. Recover the
  installation with `brew reinstall jarvis`.
