"""jarvis MCP stdio server: thin tool wrappers around QueryService, the
Zoekt search client, and the package dependency graph.

Registers 10 tools: documentSymbols, goToDefinition, findReferences,
callHierarchy, typeHierarchy, getIndexStatus, searchCode, semanticSearch, blastRadius, indexRepo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from jarvis import config, jobs, syntax, syntax_index
from jarvis.graph import GraphStore, blast_radius
from jarvis.index_reader import IndexNotFoundError
from jarvis.query import CapabilityUnavailableError, FreshnessSnapshot, QueryService
from jarvis.registry import DEGRADED_STATUS, RegisteredRepo, origin_of, recovery_for
from jarvis.search import ZoektLifecycle, search_zoekt, zoekt_repo_documents
from jarvis.symbols import AmbiguousSymbolError

mcp = FastMCP("jarvis")
_query_service: QueryService | None = None
_zoekt_lifecycle: ZoektLifecycle | None = None
_graph_store: GraphStore | None = None


# Handles for index children this server spawned. Retained so observation can
# reap them: `start_new_session=True` does not double-fork, so an exited child
# stays our zombie and `os.kill(pid, 0)` would report it alive forever. Lost
# on server restart by design -- `jobs.job_state` falls back to the launch
# record's age for that case.
_launched: dict[str, subprocess.Popen] = {}


def index_cli_module():
    """Deferred `index_cli` import. It pulls in the whole indexing pipeline,
    so the reader must not pay for it at module import; tests also
    monkeypatch through this seam."""
    from jarvis import index_cli

    return index_cli


def _jarvis_bin() -> str:
    """Absolute path to the `jarvis` console script.

    PATH is not reliable here: MCP clients spawn servers with sanitized
    environments, and `uv tool install` puts the script in a bin directory
    that may not be on it. The interpreter's own directory is correct for
    both venv and `uv tool` layouts, so try that first.
    """
    candidate = Path(sys.executable).parent / "jarvis"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("jarvis")
    if found is not None:
        return found
    raise RuntimeError(
        "the `jarvis` command could not be located next to this interpreter "
        f"({Path(sys.executable).parent}) or on PATH; brew reinstall jarvis"
    )


def _service() -> QueryService:
    global _query_service
    if _query_service is None:
        _query_service = QueryService(config.new_connection_cache())
    return _query_service


def _zoekt() -> ZoektLifecycle:
    global _zoekt_lifecycle
    if _zoekt_lifecycle is None:
        data_dir = config.data_dir()
        _zoekt_lifecycle = ZoektLifecycle(index_dir=data_dir / ".zoekt", data_dir=data_dir)
    return _zoekt_lifecycle


def _graph() -> GraphStore:
    global _graph_store
    if _graph_store is None:
        _graph_store = GraphStore(config.data_dir() / "registry.db")
    return _graph_store


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _freshness_fields(snapshot: FreshnessSnapshot | None) -> dict[str, Any]:
    # `None` means the service reported no snapshot at all; honest
    # degradation is "no freshness fields", never a crash.
    return {} if snapshot is None else _json_safe(asdict(snapshot))


def _registry_entry(repo: str) -> RegisteredRepo | None:
    """Best-effort full-row registry lookup. Any failure returns None so a
    broken registry degrades the message rather than replacing one error
    with another. The `Registry` class stays a deferred import; the row
    shape and the origin helpers are plain module-level imports."""
    try:
        from jarvis.registry import Registry

        registry = Registry(config.data_dir() / "registry.db")
        try:
            return registry.get(repo)
        finally:
            registry.close()
    except Exception:
        return None


def _registry_status(repo: str) -> str | None:
    """Best-effort registry lookup for error messaging only. Any failure
    returns None so a broken registry degrades the message rather than
    replacing one error with another."""
    entry = _registry_entry(repo)
    return entry.status if entry is not None else None


def _search_coverage_fields(repo: str) -> dict[str, Any]:
    """Whether Zoekt currently holds as many documents as git tracked at the
    last index.

    This is what makes a truncated index visible. The shards that were lost
    in the August 2026 incident were deleted *after* a successful index, so
    no index-time check could have caught it — only a comparison made when
    the index is consulted.

    Never raises: a coverage probe must not turn a working status response
    into an error. Any failure reports `null` with a reason.
    """
    try:
        from jarvis.registry import Registry

        registry = Registry(config.data_dir() / "registry.db")
        try:
            entry = registry.get(repo)
        finally:
            registry.close()
        if entry is None or entry.tracked_files is None:
            return {
                "searchCoverage": None,
                "searchCoverageReason": (
                    "no tracked-file count recorded — reindex this repo to enable "
                    "the coverage check"
                ),
            }
        base_url = _zoekt_base_url_if_running()
        if base_url is None:
            return {
                "searchCoverage": None,
                "searchCoverageReason": "zoekt-webserver not running",
            }
        indexed = zoekt_repo_documents(base_url, repo)
        if indexed is None:
            return {
                "searchCoverage": None,
                "searchCoverageReason": f"zoekt has no index for {repo}",
            }
        return {
            "searchCoverage": {
                "expected": entry.tracked_files,
                "indexed": indexed,
                # Greater-than is legitimate (multi-branch content), so this is
                # a floor check, not equality.
                "complete": indexed >= entry.tracked_files,
            }
        }
    except Exception as exc:
        return {"searchCoverage": None, "searchCoverageReason": str(exc)}


def _ctags_available() -> bool:
    """zoekt auto-discovers universal-ctags on PATH (or $CTAGS_COMMAND) at
    index time; shards built without it carry no symbol sections, so sym:
    queries and zoekt's symbol-definition ranking silently do nothing.
    Reports the tool's presence, not per-shard truth: existing shards stay
    symbol-less until a reindex after installation."""
    return bool(os.environ.get("CTAGS_COMMAND")) or shutil.which("universal-ctags") is not None


def _capability_fields(repo: str, indexed: bool, freshness: FreshnessSnapshot | None) -> dict[str, Any]:
    """The two orthogonal layers a status consumer needs (D-15):
    `last_index_run` reports what the latest run did (registry-row truth),
    `capabilities.*` reports what the system can do right now (on-disk
    truth). Navigation availability is the caller's `indexed` — the pointer
    read — never the row's status (D-07): a failed run with a live pointer
    is both outcome='failed' and navigation available-but-stale.

    Never raises: like `_search_coverage_fields`, any derivation failure
    degrades the capability fields to nulls with a reason rather than
    turning a working status response into an error. Purely filesystem and
    registry-row reads — no ZoektLifecycle call, no subprocess, so a status
    call never starts a webserver (A5). The payload is built key by key,
    never `asdict(entry)`: `status_stderr` must not leak into an MCP
    response (it can be megabytes)."""
    try:
        entry = _registry_entry(repo)
        origin = origin_of(entry) if entry is not None else None

        last_index_run = {
            # Resolution #3: the registry status string verbatim — no new
            # outcome enum to maintain; 'partial' stays success-family.
            "outcome": entry.status if entry is not None else None,
            "origin": origin,
            "reason": entry.status_reason if entry is not None else None,
            "recovery": recovery_for(entry) if entry is not None else None,
        }

        if indexed:
            last_run_failed = entry is not None and entry.status == "failed"
            stale_reported = freshness is not None and freshness.stale
            if last_run_failed or stale_reported:
                commit = freshness.commit if freshness is not None else None
                nav_reason = (
                    f"stale — indexed at {commit}" if commit else "stale — indexed commit unknown"
                )
            else:
                nav_reason = None
            nav_recovery = None
        else:
            if entry is not None and entry.status == DEGRADED_STATUS:
                # Spec TSI-06/§12: name the actual SCIP stage cause so a
                # degraded publish is visible at the MCP surface;
                # recovery_for supplies the `--scip`/reindex retry verb.
                nav_reason = entry.status_reason or "baseline published — SCIP enrichment unavailable"
            elif entry is None:
                nav_reason = "no published index"
            else:
                nav_reason = None
            nav_recovery = recovery_for(entry) if entry is not None else None

        zoekt_dir = config.data_dir() / ".zoekt"
        # The `_v` guard is load-bearing: without it "api" would match
        # "api-gateway"'s shards (`_sweep_zoekt_tmp_orphans` relies on the
        # same idiom). Pure glob — never a webserver probe.
        search_available = zoekt_dir.is_dir() and any(zoekt_dir.glob(f"{repo}_v*.zoekt"))
        semantic_available = entry is not None and entry.semantic_indexed_at is not None

        # Task 5 (spec TSI-06 "Live capabilities"): per-tool provider
        # availability plus syntax extraction counts, derived from the
        # already-cached connection/facts — no subprocess, no Zoekt spawn,
        # no grammar load, so this never violates the "never raises"/
        # "never spawns" status contract.
        facts = None
        has_syntax = False
        if indexed:
            try:
                conn = _service().connection(repo)
                facts = syntax_index.read_snapshot_facts(conn)
                has_syntax = syntax_index.has_syntax_tables(conn)
            except Exception:
                facts = None
                has_syntax = False

        def _tool_capability(providers: list[str], capability_label: str) -> dict[str, Any]:
            available = indexed and bool(providers)
            if available:
                reason = None
                recovery = None
            elif not indexed:
                reason = nav_reason or "no published index"
                recovery = nav_recovery
            else:
                reason = f"no {capability_label} data in this snapshot"
                recovery = f"jarvis reindex {repo} --scip"
            return {"available": available, "providers": providers, "reason": reason, "recovery": recovery}

        syntax_providers = ["tree-sitter"] if has_syntax else []
        doc_providers = (["scip"] if facts is not None and facts.scip_outlines else []) + syntax_providers
        def_providers = (["scip"] if facts is not None and facts.scip_definitions else []) + syntax_providers
        ref_providers = ["scip"] if facts is not None and facts.scip_references else []
        call_providers = ["scip"] if facts is not None and facts.scip_calls else []
        type_providers = ["scip"] if facts is not None and facts.scip_types else []

        tools_capability = {
            "documentSymbols": _tool_capability(doc_providers, "SCIP outline or syntax"),
            "goToDefinition": _tool_capability(def_providers, "SCIP definition or syntax"),
            "findReferences": _tool_capability(ref_providers, "SCIP occurrence"),
            "callHierarchy": _tool_capability(call_providers, "SCIP call-edge"),
            "typeHierarchy": _tool_capability(type_providers, "SCIP relationship"),
        }

        counts = facts.syntax_counts if facts is not None else None
        syntax_capability = {
            "available": has_syntax,
            "parsed": counts.parsed if counts is not None else 0,
            "partial": counts.partial if counts is not None else 0,
            "failed": counts.failed if counts is not None else 0,
            "skipped": counts.skipped if counts is not None else 0,
            "unsupported": counts.unsupported if counts is not None else 0,
            "extractionIdentity": str(syntax.SYNTAX_EXTRACTOR_VERSION) if has_syntax else None,
        }

        return {
            "last_index_run": last_index_run,
            "capabilities": {
                "navigation": {"available": indexed, "reason": nav_reason, "recovery": nav_recovery},
                "search": {
                    "available": search_available,
                    "reason": None if search_available else "no zoekt shards on disk",
                    "ctagsInstalled": _ctags_available(),
                },
                "semantic": {
                    "available": semantic_available,
                    "reason": (
                        None if semantic_available
                        else "semantic index not built for this repo (requires the `semantic` extra)"
                    ),
                },
                "tools": tools_capability,
                "syntax": syntax_capability,
            },
        }
    except Exception as exc:
        return {"last_index_run": None, "capabilities": None, "capabilitiesReason": str(exc)}


def _error_payload(repo: str, exc: Exception) -> dict[str, Any]:
    """Turn a missing SCIP index into an explanation when the registry
    row explains the repo's state. Every other error passes through
    unchanged, so this never hides a real fault.

    D-14: when the registry row explains the state, the IndexNotFoundError
    branch also carries `state` (origin slug), `cause` (one-line reason),
    and `recovery` (derived command) as structured keys alongside the prose
    `error` string — additive, so prose-only clients keep working. Those
    row-derived keys appear only when a row with an origin exists; Task 8:
    `recoveryTool`/`recoveryToolArgs` (naming the `indexRepo` tool) are
    emitted on every IndexNotFoundError payload, row or not.
    `status_stderr` never enters a payload (it can be megabytes).

    Task 5 (spec TSI-05): `CapabilityUnavailableError` — a SCIP-only tool
    with no usable capability in this snapshot, or an opaque `syntax:`
    identifier passed to one — renders `requiredCapability`/`reason`/
    `recovery` alongside the established `error` string, never an empty
    array that would imply an exhaustive search."""
    if isinstance(exc, CapabilityUnavailableError):
        payload: dict[str, Any] = {
            "error": str(exc),
            "requiredCapability": exc.capability,
            "reason": exc.reason,
            "recovery": exc.recovery,
        }
        if exc.freshness is not None:
            payload.update(_freshness_fields(exc.freshness))
        return payload
    if isinstance(exc, IndexNotFoundError):
        # Spec §12: the old search-only explanation branch is gone -- no
        # writer produces that status, and capability facts come from the
        # snapshot, not the row. The error passes through; rows that do
        # explain themselves gain the structured keys below.
        #
        # `recoveryTool` is emitted whether or not a row exists -- and a
        # never-indexed repo has no row, which is precisely the case an agent
        # hits first. The prose `recovery` above it names a shell command only
        # a human can run; this names a tool the caller can invoke itself.
        # `path` cannot be filled in here: slug -> path needs a registry row.
        entry = _registry_entry(repo)
        payload = {"error": str(exc)}
        origin = origin_of(entry) if entry is not None else None
        if origin is not None:
            payload["state"] = origin
            if entry.status_reason:
                payload["cause"] = entry.status_reason
            recovery = recovery_for(entry)
            if recovery is not None:
                payload["recovery"] = recovery
        payload["recoveryTool"] = "indexRepo"
        payload["recoveryToolArgs"] = {
            "path": "<the repo's local git working directory>"
        }
        return payload
    if isinstance(exc, AmbiguousSymbolError):
        hint = exc.candidates[0].dotted_path if exc.candidates else exc.query
        candidates_payload = []
        for c in exc.candidates:
            entry_payload = {"symbol": c.symbol, "dottedPath": c.dotted_path, "kind": str(c.kind), "source": c.source}
            if c.location is not None:
                entry_payload["location"] = _json_safe(asdict(c.location))
            candidates_payload.append(entry_payload)
        return {
            "error": (
                f"{exc.query!r} is ambiguous in {repo} ({exc.total} matches). "
                f"Retry with a qualifier, e.g. {hint!r}."
            ),
            # A structured list, not prose inside `error`, so the caller can
            # act on it without parsing English.
            "candidates": candidates_payload,
            "candidateTotal": exc.total,
        }
    return {"error": str(exc)}


def _resolved_fields(symbol: str, resolved: str) -> dict[str, Any]:
    """`resolvedSymbol` appears only when resolution changed the input, so
    callers passing full SCIP symbols see an unchanged response shape."""
    return {} if resolved == symbol else {"resolvedSymbol": resolved}


@mcp.tool(name="documentSymbols")
def document_symbols(repo: str, path: str) -> dict[str, Any]:
    """List every symbol (with its range) defined in `path` within `repo`.
    Served by the file's SCIP outline when usable, otherwise by real
    Tree-sitter syntax declarations from the same snapshot — routing is
    automatic and per-file. A syntax-served response carries a `coverage`
    object describing that file's parse outcome."""
    try:
        result = _service().get_document_symbols(repo, path)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return _error_payload(repo, exc)
    coverage_field = {"coverage": _json_safe(asdict(result.coverage))} if result.coverage is not None else {}
    if result.error is not None:
        return {"error": result.error, "path": path, **coverage_field, **_freshness_fields(result.freshness)}
    return {
        "path": path,
        "symbols": [_json_safe(asdict(e)) for e in result.entries],
        **coverage_field,
        **_freshness_fields(result.freshness),
    }


@mcp.tool(name="goToDefinition")
def go_to_definition(repo: str, symbol: str) -> dict[str, Any]:
    """Resolve `symbol`'s definition location(s) within `repo`. `symbol`
    may be a bare name (`Greeter`), a qualified name (`Greeter.greet`), a
    full SCIP symbol string, or an opaque `syntax:` identifier returned by
    a prior call. A full SCIP identifier resolves only through SCIP; a
    `syntax:` identifier resolves only through its exact declaration; a
    bare/qualified name searches both SCIP definitions and syntax
    declarations from files without usable SCIP definition coverage."""
    try:
        resolved, locations, freshness = _service().resolve_definition(repo, symbol)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return _error_payload(repo, exc)
    return {
        "symbol": symbol,
        **_resolved_fields(symbol, resolved),
        "definitions": [_json_safe(asdict(loc)) for loc in locations],
        **_freshness_fields(freshness),
    }


@mcp.tool(name="findReferences")
def find_references(repo: str, symbol: str) -> dict[str, Any]:
    """Every occurrence of `symbol` within `repo`, definition sites
    included. `symbol` may be a bare name (`Greeter`), a qualified name
    (`Greeter.greet`), or a full SCIP symbol string. SCIP-only: requires
    real SCIP occurrence data and rejects an opaque `syntax:` identifier
    with a capability error, even when other files have SCIP coverage —
    it never implements lexical references or treats a syntax name match
    as a reference."""
    try:
        resolved, locations, freshness = _service().find_references(repo, symbol)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return _error_payload(repo, exc)
    return {
        "symbol": symbol,
        **_resolved_fields(symbol, resolved),
        "references": [_json_safe(asdict(loc)) for loc in locations],
        **_freshness_fields(freshness),
    }


@mcp.tool(name="callHierarchy")
def call_hierarchy(repo: str, symbol: str) -> dict[str, Any]:
    """Single-level incoming/outgoing call hierarchy for `symbol` within
    `repo`. `symbol` may be a bare name (`Greeter`), a qualified name
    (`Greeter.greet`), or a full SCIP symbol string. SCIP-only: requires
    real SCIP occurrence/enclosing-range data and rejects an opaque
    `syntax:` identifier with a capability error — call edges are never
    derived from syntax."""
    try:
        resolved, incoming, outgoing, freshness = _service().call_hierarchy(repo, symbol)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return _error_payload(repo, exc)
    return {
        "symbol": symbol,
        **_resolved_fields(symbol, resolved),
        "incomingCalls": [_json_safe(asdict(e)) for e in incoming],
        "outgoingCalls": [_json_safe(asdict(e)) for e in outgoing],
        **_freshness_fields(freshness),
    }


@mcp.tool(name="typeHierarchy")
def type_hierarchy(repo: str, symbol: str) -> dict[str, Any]:
    """Single-level super/subtypes for `symbol` within `repo`. `symbol` may
    be a bare name (`Greeter`), a qualified name (`Greeter.greet`), or a full
    SCIP symbol string. SCIP-only: requires real SCIP relationship data and
    rejects an opaque `syntax:` identifier with a capability error — type
    edges are never derived from syntax.

    Returns an explicit capability error when the index carries no
    relationship data — unpatched `scip expt-convert` (upstream through
    v0.9.0) does not populate `global_symbols.relationships`, so an empty
    result would wrongly imply the symbol has no supertypes. Reinstalling
    the jarvis Homebrew package and reindexing makes this self-heal."""
    try:
        resolved, supertypes, subtypes, freshness = _service().type_hierarchy(repo, symbol)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return _error_payload(repo, exc)
    return {
        "symbol": symbol,
        **_resolved_fields(symbol, resolved),
        "supertypes": [_json_safe(asdict(e)) for e in supertypes],
        "subtypes": [_json_safe(asdict(e)) for e in subtypes],
        **_freshness_fields(freshness),
    }


def _indexing_fields(repo: str, indexed: bool) -> dict[str, Any]:
    """The `indexing` block, or nothing when no run is in flight.

    Reaping happens inside `jobs.job_state` via the retained handle: a
    tracked child's exit code is stronger evidence than any liveness check,
    and reaping is what stops an exited-but-unwaited child from reading as
    alive forever.

    Never raises: a status response must survive a bug in here.
    """
    try:
        entry = _registry_entry(repo)
        state = jobs.job_state(
            repo, indexed=indexed,
            registry_status=entry.status if entry is not None else None,
            # Attempt correlation: a launch record newer than the row belongs
            # to the CURRENT attempt, so stale `failed` / `indexing` state
            # from a previous run cannot terminate this poll loop.
            registry_updated_at=entry.last_indexed if entry is not None else None,
            child=_launched.get(repo),
        )
    except Exception:  # Broad on purpose, same contract as the helpers above.
        return {}
    if state is None:
        return {}
    block: dict[str, Any] = {"state": state.state}
    if state.pid is not None:
        block["pid"] = state.pid
    if state.exit_code is not None:
        block["exitCode"] = state.exit_code
    if state.log is not None:
        block["log"] = state.log
    return {"indexing": block}


@mcp.tool(name="getIndexStatus")
def get_index_status(repo: str, repo_path: str | None = None) -> dict[str, Any]:
    """Whether `repo` has a published index, and its freshness. Pass
    `repo_path` (the repo's local git working directory) to compare the
    published commit against `git rev-parse HEAD`; omitted, freshness is
    reported without a staleness comparison. `searchCoverage` reflects git
    HEAD at last index time, not the working tree — it does not account for
    uncommitted or untracked changes.

    While an index is being built, an `indexing` block reports its state:
    `starting`, `running`, `failed-at-startup`, or `abandoned`. The first two
    mean keep polling; the last two are terminal."""
    try:
        indexed, freshness = _service().get_index_status(repo, repo_path)
    except Exception as exc:
        return {"error": str(exc)}
    try:
        capability_fields = _capability_fields(repo, indexed, freshness)
    except Exception:
        # Belt over `_capability_fields`' own never-raise: even a bug in the
        # helper must not kill the published status response.
        capability_fields = {"last_index_run": None, "capabilities": None}
    return {"repo": repo, "indexed": indexed, "status": _registry_status(repo),
            **_freshness_fields(freshness), **_search_coverage_fields(repo),
            **_indexing_fields(repo, indexed), **capability_fields}


@mcp.tool(name="indexRepo")
def index_repo_tool(path: str, semantic: bool = False,
                    scip: bool | None = None) -> dict[str, Any]:
    """Build an index for the git repository at `path`, so the other tools
    have something to read.

    Takes a filesystem `path` rather than a `repo` slug -- deliberately the
    only tool that does. Slug derivation is one-way, so the server cannot
    turn a slug into a path for a repo it has never indexed; `path` is
    exactly what the caller knows and the registry lacks. The returned `repo`
    is the slug every other tool accepts.

    Returns immediately with `state: "starting"`; the index runs in a
    detached child. Poll `getIndexStatus` until it reports `indexed: true` or
    a terminal `indexing.state` (`failed-at-startup`, `abandoned`) -- the
    loop always terminates.

    `semantic` defaults to False: the semantic stage loads embedding weights
    (a multi-gigabyte download on first use), which is not something a single
    tool call should trigger implicitly. `scip=None` leaves the repo's
    persisted SCIP choice alone.
    """
    try:
        return _spawn_index(path, semantic=semantic, scip=scip)
    except Exception as exc:  # Broad on purpose: the MCP boundary never raises.
        return {"error": str(exc)}


def _spawn_index(path: str, *, semantic: bool,
                 scip: bool | None) -> dict[str, Any]:
    """`indexRepo`'s body. Every pre-flight check runs before anything is
    spawned, so a caller gets an actionable error instead of a log file to go
    read."""
    index_cli = index_cli_module()
    jarvis_bin = _jarvis_bin()
    if shutil.which("zoekt-git-index") is None:
        # Stage 4 of the pipeline is required and fails the run without it
        # (index_cli.py's "Zoekt (required; failure fails the run)"). The
        # tree-sitter baseline needs no LANGUAGE toolchain, but it is not
        # binary-free.
        return {
            "error": "zoekt-git-index was not found on PATH; a first index "
                     "cannot be built without it",
            "recovery": "brew reinstall jarvis, then retry indexing",
        }
    resolved = Path(path).expanduser().resolve()
    index_cli.ensure_git_repo(resolved)

    registry = index_cli.Registry(config.data_dir() / "registry.db")
    try:
        slug = index_cli.resolve_slug_for_path(registry, resolved)
        entry = registry.get(slug)
    finally:
        registry.close()

    probe = jobs.lock_state(slug)
    if probe.held:
        return {"repo": slug, "path": str(resolved), "status": "indexing",
                "state": "running", "pid": probe.pid,
                "log": str(config.index_log(slug)), "alreadyRunning": True}
    existing_child = _launched.get(slug)
    if existing_child is not None and existing_child.poll() is None:
        # The lock probe alone leaves a window: a child spawned moments ago
        # may not have taken the lock yet, and spawning a second one would
        # both lose the first handle (leaking a zombie we can no longer reap)
        # and guarantee one child exits rc 1 on the lock.
        return {"repo": slug, "path": str(resolved), "status": "indexing",
                "state": "starting", "pid": existing_child.pid,
                "log": str(config.index_log(slug)), "alreadyRunning": True}

    flags: list[str] = []
    if not semantic:
        flags.append("--no-semantic")
    if scip is True:
        flags.append("--scip")
    elif scip is False:
        flags.append("--no-scip")

    log_path = config.index_log(slug)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Before Popen, never after: a fast child (tiny repo, warm caches) can
    # acquire the lock and register before the parent gets another turn, and
    # a post-spawn write would then recreate state nobody removes. The record
    # is written once and never mutated, so there is no handoff to serialize.
    jobs.write_launch_record(slug, log_path)

    log_file = open(log_path, "ab")
    try:
        child = subprocess.Popen(
            # `--slug` is explicit and NOT optional: `resolve_slug_for_path`
            # may have returned a slug the child would never derive on its own
            # (a repo registered under a custom --slug resolves by path, not
            # by basename). Without it the child derives the basename, hits
            # `_reject_duplicate_slug_for_path`, and dies -- while this tool
            # has already reported the custom slug to the caller.
            [jarvis_bin, "index", str(resolved), "--slug", slug, *flags],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "JARVIS_DATA_DIR": str(config.data_dir())},
        )
    finally:
        log_file.close()  # the child keeps its own duplicated fd
    _launched[slug] = child

    payload: dict[str, Any] = {
        "repo": slug, "path": str(resolved), "status": "indexing",
        "state": "starting", "pid": child.pid, "log": str(log_path),
    }
    if entry is not None:
        payload["reindex"] = True
    return payload

@mcp.tool(name="searchCode")
def search_code(query: str, repo: str | None = None) -> dict[str, Any]:
    """Lexical code search via an embedded Zoekt index (lazy-started on
    first call). `repo`, if given, is applied as a Zoekt `r:` query filter
    scoping results to that one indexed repo; omitted, results span every
    indexed repo. Results answer over the last published index snapshot,
    not the working tree — `indexedAt` says when that was. Zoekt query
    syntax is live: `sym:`, `file:`, `lang:`, `case:` filters, `-term`
    negation, quoted phrases, and `(a or b)` grouping; `sym:` needs
    universal-ctags installed at index time. `truncated` is true when
    zoekt found more matches than the returned cap."""
    scoped_query = f"r:{repo} {query}" if repo else query
    try:
        base_url = _zoekt().ensure_running()
        result = search_zoekt(base_url, scoped_query)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return {"error": str(exc)}
    entry = _registry_entry(repo) if repo else None  # best-effort; None-safe
    return {
        "query": query,
        "hits": [
            {"repo": hit.repo, "path": hit.path, "lineNumber": hit.line_number, "lineText": hit.line_text}
            for hit in result.hits
        ],
        "totalMatches": result.total_matches,
        "fileCount": result.file_count,
        "returned": len(result.hits),
        "truncated": result.total_matches > len(result.hits),
        "indexedAt": entry.last_indexed.isoformat() if entry is not None else None,
    }


def _zoekt_base_url_or_none() -> str | None:
    """Hybrid search wants Zoekt but must not require it — a Zoekt spawn
    failure degrades semanticSearch to vector-only rather than erroring."""
    try:
        return _zoekt().ensure_running()
    except Exception:
        return None


def _zoekt_base_url_if_running() -> str | None:
    """Module-level indirection so `_search_coverage_fields` is testable
    without a real webserver."""
    return _zoekt().base_url_if_running()


def _scip_conn_or_none(repo: str):
    """The symbol signal wants a SCIP index but must not require one — a
    repo without a published snapshot (or any failure) degrades
    semanticSearch to the vector+zoekt signals, mirroring
    _zoekt_base_url_or_none."""
    try:
        return _service().connection(repo)
    except Exception:
        return None


@mcp.tool(name="semanticSearch")
def semantic_search_tool(repo: str, query: str, limit: int = 10) -> dict[str, Any]:
    """Natural-language code search over `repo`: embeds `query`, retrieves
    top vector matches from the repo's semantic index, and fuses them with
    Zoekt lexical hits and SCIP symbol-definition matches (when a SCIP
    index exists) via reciprocal rank fusion. `sources` on each result
    names which signal(s) contributed; a symbol-only hit carries
    `content=""` (SCIP stores no source text) with `symbolName` set to the
    definition's dotted path. Requires the repo to have been indexed with
    the `semantic` extra installed."""
    from jarvis import semantic  # deferred: tool must exist even without the extra

    try:
        return semantic.semantic_search(repo, query, limit,
                                        zoekt_base_url=_zoekt_base_url_or_none(),
                                        scip_conn=_scip_conn_or_none(repo))
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return {"error": str(exc)}


@mcp.tool(name="blastRadius")
def blast_radius_tool(repo: str, symbol_or_package: str) -> dict[str, Any]:
    """2-hop bounded BFS over the package dependency graph: every other
    indexed repo whose package directly (1 hop) or transitively through one
    intermediary (2 hops) depends on `symbol_or_package` as registered for
    `repo` (built by `jarvis index`, e.g. `"npm:@scope/name"`). The
    graph has no per-node timestamp, so freshness is always reported as
    `unknown` here — an honest limitation, not a bug."""
    try:
        result = blast_radius(_graph(), repo, symbol_or_package)
    except Exception as exc:
        # Broad on purpose — keeps every tool's error shape the same {"error": ...} dict.
        return {"error": str(exc)}
    return {
        "repo": repo,
        "symbolOrPackage": symbol_or_package,
        "dependents": [_json_safe(asdict(d)) for d in result.dependents],
        **_freshness_fields(result.freshness),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
