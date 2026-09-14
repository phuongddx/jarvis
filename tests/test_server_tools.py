"""MCP in-memory client session: list_tools returns the 10 tools, and
roundtrips for documentSymbols, searchCode, semanticSearch, and blastRadius against the
synthetic fixture / a fake zoekt-webserver / an in-memory package graph."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from jarvis import config, query, server
from jarvis.graph import GraphStore
from jarvis.index_reader import IndexNotFoundError
from jarvis.search import ZoektLifecycle
from jarvis.symbols import AmbiguousSymbolError, Candidate, DescriptorKind
from tests.fixtures.synthetic_index import CLASS_SYMBOL, DOC_GREETER, METHOD_SYMBOL, build_published_index
from tests.test_query import build_combined_snapshot, build_syntax_only_snapshot

REPO = "toy-repo"

EXPECTED_TOOLS = {
    "documentSymbols",
    "goToDefinition",
    "findReferences",
    "callHierarchy",
    "typeHierarchy",
    "getIndexStatus",
    "searchCode",
    "semanticSearch",
    "blastRadius",
    "indexRepo",
}


@pytest.fixture(autouse=True)
def _wired_query_service(tmp_path: Path, monkeypatch):
    # Isolates config.data_dir() to tmp_path so _error_payload's registry
    # lookup (added for search-only explanations) never touches the real
    # ~/.jarvis/registry.db when a test's repo raises IndexNotFoundError.
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    build_published_index(tmp_path, config.PROJECT, REPO, config.BRANCH)
    service = query.QueryService(config.new_connection_cache(tmp_path))
    monkeypatch.setattr(server, "_query_service", service)
    yield
    monkeypatch.setattr(server, "_query_service", None)


@pytest.fixture(autouse=True)
def _no_leaked_children():
    """`_spawn_index` mutates `server._launched` directly, so a spawn test
    leaves a live handle behind and the next test's spawn hits the
    duplicate-spawn guard and returns `alreadyRunning` instead of spawning.
    Clear it around every test in this module."""
    server._launched.clear()
    yield
    server._launched.clear()


@pytest.mark.anyio
async def test_document_symbols_roundtrip():
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("documentSymbols", {"repo": REPO, "path": DOC_GREETER})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)
        assert [s["displayName"] for s in payload["symbols"]] == ["Greeter", "greet", "DEFAULT_NAME", "sayHi"]


@pytest.mark.anyio
async def test_go_to_definition_missing_repo_returns_error_payload():
    """A repo that was never registered at all is a plain "index not found"
    -- distinct from the search-only explanation, which only applies when
    the registry records status == SEARCH_ONLY_STATUS for that repo."""
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("goToDefinition", {"repo": "never-published", "symbol": "x"})
        payload = json.loads(result.content[0].text)
        assert "error" in payload
        assert "search-only" not in payload["error"]


@pytest.mark.anyio
async def test_unexpected_exception_still_returns_structured_error_payload(monkeypatch):
    """Every tool must fail the same way — a `{"error": ...}` dict, not an
    MCP-level `isError` text result — regardless of which exception type
    the underlying service raises."""

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    # goToDefinition routes through resolve_definition (Task 5), the sole
    # call site that does resolution + lookup for that tool.
    monkeypatch.setattr(server.QueryService, "resolve_definition", _boom)
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("goToDefinition", {"repo": REPO, "symbol": "x"})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)
        assert payload == {"error": "boom"}


@pytest.mark.anyio
async def test_ambiguous_symbol_error_reaches_client_as_candidates_payload(monkeypatch):
    """Full-stack check that `AmbiguousSymbolError` -- raised during symbol
    resolution -- reaches a real MCP tool call as a structured
    `{"candidates": ..., "candidateTotal": ...}` payload. `resolve()` and
    `_error_payload()` are each covered directly elsewhere in this file /
    test_symbols.py, but neither exercises the actual tool-invocation path,
    which is what a caller of goToDefinition experiences."""
    candidates = (
        Candidate(symbol="sym-a", dotted_path="a.C.dup", kind=DescriptorKind.METHOD),
        Candidate(symbol="sym-b", dotted_path="b.D.dup", kind=DescriptorKind.METHOD),
    )

    def _ambiguous(*args, **kwargs):
        raise AmbiguousSymbolError("dup", candidates, 2)

    # goToDefinition routes through resolve_definition (Task 5) -- that is
    # the call site to raise from to exercise the real wiring end to end.
    monkeypatch.setattr(server.QueryService, "resolve_definition", _ambiguous)
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("goToDefinition", {"repo": REPO, "symbol": "dup"})
        assert result.isError is not True
        payload = json.loads(result.content[0].text)

    assert payload["candidateTotal"] == 2
    assert payload["candidates"] == [
        {"symbol": "sym-a", "dottedPath": "a.C.dup", "kind": "METHOD", "source": "scip"},
        {"symbol": "sym-b", "dottedPath": "b.D.dup", "kind": "METHOD", "source": "scip"},
    ]
    assert "definitions" not in payload


_FAKE_ZOEKT_SEARCH_SCRIPT = """\
#!PYTHON_SHEBANG_PLACEHOLDER
import base64
import http.server
import json
import socketserver
import sys

port = int(sys.argv[sys.argv.index("-listen") + 1].lstrip(":"))

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        line = base64.b64encode(b"def greet(name):").decode()
        body = json.dumps({
            "Result": {"Files": [{"Repository": "toy-repo", "FileName": "toy/greeter.py",
                                   "LineMatches": [{"LineNumber": 5, "Line": line}]}]}
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

class Server(socketserver.TCPServer):
    # See the matching note in tests/test_search.py: without SO_REUSEADDR a
    # fixed port still in TIME_WAIT from a previous run cannot be rebound, so
    # this process exits with EADDRINUSE and the test reports the server as
    # having "exited immediately".
    allow_reuse_address = True

with Server(("127.0.0.1", port), Handler) as httpd:
    httpd.serve_forever()
"""


@pytest.mark.anyio
async def test_search_code_roundtrip(tmp_path: Path, monkeypatch):
    script_path = tmp_path / "fake-zoekt-webserver"
    # An absolute shebang (vs. `#!/usr/bin/env python3`) avoids PATH-resolution
    # flakiness spawning this fake server as a subprocess — this only affects
    # the test double; the real ZoektLifecycle always spawns the actual
    # zoekt-webserver binary directly, never through a shebang lookup.
    script_path.write_text(
        _FAKE_ZOEKT_SEARCH_SCRIPT.replace("PYTHON_SHEBANG_PLACEHOLDER", sys.executable), encoding="utf-8"
    )
    script_path.chmod(0o755)

    lifecycle = ZoektLifecycle(
        index_dir=tmp_path / "zoekt-index",
        data_dir=tmp_path / "zoekt-data",
        port=16090,
        binary=[sys.executable, str(script_path)],
    )
    monkeypatch.setattr(server, "_zoekt_lifecycle", lifecycle)
    try:
        async with create_connected_server_and_client_session(server.mcp) as client:
            result = await client.call_tool("searchCode", {"query": "greet"})
            payload = json.loads(result.content[0].text)
            assert payload["totalMatches"] == 1
            assert payload["returned"] == 1
            assert payload["truncated"] is False
            assert payload["hits"][0]["repo"] == "toy-repo"
            assert payload["hits"][0]["lineText"] == "def greet(name):"
    finally:
        lifecycle.stop()
        monkeypatch.setattr(server, "_zoekt_lifecycle", None)


@pytest.mark.anyio
async def test_blast_radius_roundtrip(tmp_path: Path, monkeypatch):
    store = GraphStore(tmp_path / "registry.db")
    seed_id = store.upsert_package(repo=REPO, name="npm:seed")
    dep_id = store.upsert_package(repo="dep-repo", name="npm:dep")
    store.add_edge(from_package_id=dep_id, to_package_id=seed_id)
    monkeypatch.setattr(server, "_graph_store", store)
    try:
        async with create_connected_server_and_client_session(server.mcp) as client:
            result = await client.call_tool("blastRadius", {"repo": REPO, "symbol_or_package": "npm:seed"})
            payload = json.loads(result.content[0].text)
            assert payload["dependents"] == [{"repo": "dep-repo", "name": "npm:dep", "hops": 1}]
            assert payload["freshness"] == "unknown"
            assert payload["commit"] is None
    finally:
        store.close()
        monkeypatch.setattr(server, "_graph_store", None)


@pytest.mark.anyio
async def test_blast_radius_unknown_package_returns_error_payload(tmp_path: Path, monkeypatch):
    store = GraphStore(tmp_path / "registry.db")
    monkeypatch.setattr(server, "_graph_store", store)
    try:
        async with create_connected_server_and_client_session(server.mcp) as client:
            result = await client.call_tool("blastRadius", {"repo": REPO, "symbol_or_package": "npm:no-such"})
            payload = json.loads(result.content[0].text)
            assert "error" in payload
    finally:
        store.close()
        monkeypatch.setattr(server, "_graph_store", None)


@pytest.mark.anyio
async def test_type_hierarchy_reports_unavailable_instead_of_empty(monkeypatch):
    """Empty arrays read as "no supertypes exist" -- a false answer.

    scip expt-convert never populates global_symbols.relationships on a real
    index, so the tool cannot answer and must say so rather than imply one.
    """

    def _unavailable(self, repo, symbol):
        _, metadata = query.get_connection(self._cache, repo)
        raise query.CapabilityUnavailableError(
            capability="typeHierarchy",
            message="typeHierarchy unavailable for this index: no symbol carries relationships data.",
            reason="no SCIP relationship data in this snapshot",
            recovery=f"jarvis reindex {repo}",
            freshness=query._freshness_snapshot(metadata),
        )

    monkeypatch.setattr(server.QueryService, "type_hierarchy", _unavailable)
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("typeHierarchy", {"repo": REPO, "symbol": "x"})
        payload = json.loads(result.content[0].text)

    assert "error" in payload, payload
    assert "relationships" in payload["error"].lower()
    assert payload["requiredCapability"] == "typeHierarchy"
    assert "supertypes" not in payload


@pytest.mark.anyio
async def test_type_hierarchy_returns_results_when_relationships_present():
    """The synthetic fixture DOES carry a contrived non-NULL relationships
    blob for `Greeter#`, so the available path is what this index exercises —
    no monkeypatching needed. Guards against the unavailable branch
    swallowing genuine results.
    """
    async with create_connected_server_and_client_session(server.mcp) as client:
        result = await client.call_tool("typeHierarchy", {"repo": REPO, "symbol": "Greeter"})
        payload = json.loads(result.content[0].text)

    assert "error" not in payload, payload
    assert "supertypes" in payload
    assert "subtypes" in payload


def test_semantic_search_tool_returns_results(monkeypatch):
    def _fake_search(repo, query, limit, zoekt_base_url=None, scip_conn=None):
        return {"query": query, "results": [], "total": 0}

    monkeypatch.setattr(server, "_zoekt_base_url_or_none", lambda: "http://x")
    monkeypatch.setattr("jarvis.semantic.semantic_search", _fake_search)
    result = server.semantic_search_tool(repo="r", query="auth logic")
    assert result == {"query": "auth logic", "results": [], "total": 0}


def test_semantic_search_tool_wraps_errors(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no semantic index for r — run jarvis reindex r")

    monkeypatch.setattr(server, "_zoekt_base_url_or_none", lambda: None)
    monkeypatch.setattr("jarvis.semantic.semantic_search", _boom)
    result = server.semantic_search_tool(repo="r", query="q")
    assert "no semantic index" in result["error"]


def test_scip_conn_or_none_returns_none_when_no_index(monkeypatch):
    """Search-only repos (IndexNotFoundError) and any other failure both
    degrade to None -- semanticSearch must not error over a missing SCIP index."""
    def _boom(*args, **kwargs):
        raise IndexNotFoundError("no index published")

    monkeypatch.setattr(server.QueryService, "connection", _boom)
    assert server._scip_conn_or_none(REPO) is None




def test_error_payload_passes_through_other_errors(tmp_path: Path, monkeypatch):
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    payload = server._error_payload("absent", RuntimeError("boom"))
    assert payload == {"error": "boom"}








def test_missing_index_names_the_recovery_tool_without_a_row(tmp_path, monkeypatch):
    """The row-less case is the one that matters: a never-indexed repo has no
    registry row, so the pre-existing structured keys never appeared."""
    from jarvis import server
    from jarvis.index_reader import IndexNotFoundError

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    payload = server._error_payload("absent", IndexNotFoundError("no published index"))

    assert payload["recoveryTool"] == "indexRepo"
    assert "path" in payload["recoveryToolArgs"]
    assert "state" not in payload  # no row to explain anything


def test_missing_index_keeps_row_explanation_and_adds_the_tool(tmp_path, monkeypatch):
    from jarvis import config, server
    from jarvis.index_reader import IndexNotFoundError
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.record_failure("app", "/tmp/app", "python",
                                origin="manual", reason="boom", stderr="trace")
    finally:
        registry.close()

    payload = server._error_payload("app", IndexNotFoundError("no published index"))

    assert payload["error"] == "no published index"
    assert payload["cause"] == "boom"
    assert payload["recovery"]  # prose recovery preserved
    assert payload["recoveryTool"] == "indexRepo"
    assert "status_stderr" not in payload
    assert "trace" not in str(payload)


def test_error_payload_does_not_mask_other_faults_on_a_degraded_repo(tmp_path: Path, monkeypatch):
    """A query fault on a degraded repo is not a degradation explanation:
    structured keys apply to the IndexNotFoundError branch only, so a
    RuntimeError keeps its existing bare shape (no masking)."""
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "unknown", "abc", DEGRADED_STATUS,
                        status_origin=ORIGIN_FALLBACK,
                        status_reason="the build produced no SCIP shards")
    finally:
        registry.close()

    payload = server._error_payload("gorepo", RuntimeError("kaboom"))
    assert payload == {"error": "kaboom"}


def test_error_payload_with_unreadable_registry_still_names_the_recovery_tool(tmp_path: Path, monkeypatch):
    """A broken registry degrades the lookup, never replaces one error with
    another (the `_registry_entry` best-effort convention) — and the
    recovery-tool keys survive the degraded lookup because they need no
    row: only the row-derived `state`/`cause`/`recovery` keys go missing."""
    from jarvis import registry as registry_module
    from jarvis.index_reader import IndexNotFoundError
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "unknown", "abc", "indexed")
    finally:
        registry.close()

    def _unusable(*args, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(registry_module, "Registry", _unusable)

    payload = server._error_payload("gorepo", IndexNotFoundError("no pointer"))
    assert payload == {
        "error": "no pointer",
        "recoveryTool": "indexRepo",
        "recoveryToolArgs": {"path": "<the repo's local git working directory>"},
    }




def test_get_index_status_reports_none_status_when_never_registered():
    result = server.get_index_status(repo="never-registered-anywhere")
    assert result["status"] is None
    assert result["indexed"] is False





def test_get_index_status_navigation_unavailable_for_degraded_reports_cause_and_recovery(tmp_path: Path, monkeypatch):
    """FALL-01 (visibility): on a degraded repo, navigation is unavailable
    AND the reason names the actual failure cause from the persisted
    status_reason — not a generic wording — with the fallback self-heal
    verb as recovery."""
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "unknown", "abc", DEGRADED_STATUS,
                        status_origin=ORIGIN_FALLBACK,
                        status_reason="indexer crashed")
    finally:
        registry.close()

    nav = server.get_index_status(repo="gorepo")["capabilities"]["navigation"]

    assert nav["available"] is False
    assert nav["reason"] == "indexer crashed"
    assert "jarvis reindex" in nav["recovery"]


def test_get_index_status_degraded_reason_defaults_when_status_reason_missing(tmp_path: Path, monkeypatch):
    """A degraded row with no persisted reason still explains itself with
    the default degraded wording instead of a None reason."""
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "unknown", "abc", DEGRADED_STATUS,
                        status_origin=ORIGIN_FALLBACK)
    finally:
        registry.close()

    nav = server.get_index_status(repo="gorepo")["capabilities"]["navigation"]

    assert nav["available"] is False
    assert nav["reason"] == "baseline published — SCIP enrichment unavailable"


def test_get_index_status_reports_last_index_run_for_a_degraded_repo(tmp_path: Path, monkeypatch):
    """Pin (D-13/D-15 verbatim contract): the registry status string flows
    through last_index_run.outcome unchanged and the fallback origin
    carries its recovery verb — zero payload reshaping."""
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "unknown", "abc", DEGRADED_STATUS,
                        status_origin=ORIGIN_FALLBACK,
                        status_reason="indexer crashed")
    finally:
        registry.close()

    last_run = server.get_index_status(repo="gorepo")["last_index_run"]

    assert last_run["outcome"] == "degraded"
    assert last_run["origin"] == "fallback"
    assert last_run["reason"] == "indexer crashed"
    assert "jarvis reindex" in last_run["recovery"]


def test_error_payload_carries_state_cause_recovery_for_a_degraded_repo(tmp_path: Path, monkeypatch):
    """Pin (D-14): the origin-key block already covers degraded rows —
    state='fallback', cause=the persisted reason, recovery present,
    alongside the unchanged `error` key. No _error_payload change needed."""
    from jarvis.registry import DEGRADED_STATUS, ORIGIN_FALLBACK, Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "unknown", "abc", DEGRADED_STATUS,
                        status_origin=ORIGIN_FALLBACK,
                        status_reason="indexer crashed")
    finally:
        registry.close()

    payload = server._error_payload("gorepo", IndexNotFoundError("no pointer"))

    assert "error" in payload
    assert payload["state"] == "fallback"
    assert payload["cause"] == "indexer crashed"
    assert "recovery" in payload


def test_get_index_status_failed_run_with_live_pointer_reports_stale_navigation(tmp_path: Path, monkeypatch):
    """D-07/D-15: atomic publish leaves the last index live when the newest
    run failed, so the payload reports outcome='failed' AND
    navigation.available=True with the stale commit named — that
    combination is correct, not a bug (Pitfall 4)."""
    from datetime import UTC, datetime

    from jarvis.query import Freshness, FreshnessSnapshot
    from jarvis.registry import ORIGIN_FAILED_HARD, Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))

    class _LivePointerStub:
        def get_index_status(self, repo, repo_path=None):
            return True, FreshnessSnapshot(
                commit="abc123",
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
                stale=True,
                freshness=Freshness.STALE,
                checked_at=datetime.now(UTC),
            )

    monkeypatch.setattr(server, "_query_service", _LivePointerStub())

    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.record_failure("gorepo", "/p", "unknown", ORIGIN_FAILED_HARD,
                                "scip-python failed", "full stderr text")
    finally:
        registry.close()

    result = server.get_index_status(repo="gorepo")

    assert result["last_index_run"]["outcome"] == "failed"
    assert result["last_index_run"]["origin"] == "failed_hard"
    nav = result["capabilities"]["navigation"]
    assert nav["available"] is True
    assert "abc123" in nav["reason"]


def test_get_index_status_search_capability_follows_zoekt_shards_on_disk(tmp_path: Path, monkeypatch):
    """A5: search availability is a pure filesystem read of the zoekt shard
    dir — the `{slug}_v*` glob with the `_v` guard, never a webserver
    probe."""
    zoekt_dir = config.data_dir() / ".zoekt"
    zoekt_dir.mkdir(parents=True)
    (zoekt_dir / "gorepo_v16.00000.zoekt").write_text("dummy shard")

    search = server.get_index_status(repo="gorepo")["capabilities"]["search"]
    assert search["available"] is True
    assert search["reason"] is None

    # The _v guard: a sibling repo's shards must not make this repo available.
    (zoekt_dir / "otherrepo_v16.00000.zoekt").write_text("dummy shard")
    (zoekt_dir / "gorepo_v16.00000.zoekt").unlink()
    search = server.get_index_status(repo="gorepo")["capabilities"]["search"]
    assert search["available"] is False
    assert search["reason"] == "no zoekt shards on disk"


def test_capability_fields_report_ctags_availability(monkeypatch, tmp_path):
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CTAGS_COMMAND", raising=False)
    monkeypatch.setattr(server.shutil, "which",
                        lambda name: None if name == "universal-ctags" else f"/usr/bin/{name}")
    fields = server._capability_fields("nosuchrepo", indexed=False, freshness=None)
    assert fields["capabilities"]["search"]["ctagsInstalled"] is False

    monkeypatch.setenv("CTAGS_COMMAND", "/opt/ctags/bin/ctags")
    fields = server._capability_fields("nosuchrepo", indexed=False, freshness=None)
    assert fields["capabilities"]["search"]["ctagsInstalled"] is True


def test_get_index_status_semantic_capability_follows_the_row(tmp_path: Path, monkeypatch):
    """A6: semantic availability derives from `semantic_indexed_at` on the
    row (recorded at index time); the reason names the missing extra —
    the semantic extra is never imported here (base install works)."""
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "python", "abc", "indexed")
        before = server.get_index_status(repo="gorepo")
        registry.mark_semantic_indexed("gorepo")
        after = server.get_index_status(repo="gorepo")
    finally:
        registry.close()

    assert before["capabilities"]["semantic"]["available"] is False
    assert "extra" in before["capabilities"]["semantic"]["reason"]
    assert after["capabilities"]["semantic"]["available"] is True


def test_get_index_status_capability_failure_degrades_to_nulls(tmp_path: Path, monkeypatch):
    """The never-raise convention: even if capability derivation itself
    blows up, the pre-existing status payload must survive with capability
    fields degraded to null — never an error response (A5)."""
    def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "_capability_fields", _boom)

    result = server.get_index_status(repo="gorepo")

    assert "error" not in result
    assert result["repo"] == "gorepo"
    assert result["indexed"] is False
    assert result["last_index_run"] is None
    assert result["capabilities"] is None


def test_get_index_status_adds_capability_fields_without_reshaping_existing_keys():
    """D-13 additive-only: repo/indexed/status/freshness/searchCoverage
    keep their names and values for a plain published index."""
    indexed_direct, freshness_direct = server._service().get_index_status(REPO)

    result = server.get_index_status(repo=REPO)

    assert result["repo"] == REPO
    assert result["indexed"] is True
    assert result["indexed"] == indexed_direct
    assert result["status"] is None  # the fixture publishes an index but registers no row
    assert result["commit"] == freshness_direct.commit
    assert result["stale"] == freshness_direct.stale
    assert "searchCoverage" in result
    assert result["capabilities"]["navigation"]["available"] is True


def test_get_index_status_capability_derivation_never_spawns(tmp_path: Path, monkeypatch):
    """A5 + T-01-08: a status call derives capabilities from the filesystem
    and the registry row only — zoekt-webserver must never be started and
    no subprocess may run."""
    import subprocess as subprocess_module

    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("gorepo", "/p", "python", "abc", "indexed")
    finally:
        registry.close()
    zoekt_dir = config.data_dir() / ".zoekt"
    zoekt_dir.mkdir(parents=True)
    (zoekt_dir / "gorepo_v16.00000.zoekt").write_text("dummy shard")

    def _forbidden(*args, **kwargs):
        raise AssertionError("a status call must not spawn a process")

    monkeypatch.setattr(subprocess_module, "run", _forbidden)
    monkeypatch.setattr(subprocess_module, "Popen", _forbidden)
    monkeypatch.setattr("jarvis.search.ZoektLifecycle.ensure_running", _forbidden)

    result = server.get_index_status(repo="gorepo")

    assert "error" not in result
    assert result["capabilities"]["search"]["available"] is True


def test_error_payload_renders_ambiguous_candidates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    candidates = (
        Candidate(symbol="sym-a", dotted_path="a.C.dup", kind=DescriptorKind.METHOD),
        Candidate(symbol="sym-b", dotted_path="b.D.dup", kind=DescriptorKind.METHOD),
    )
    payload = server._error_payload(REPO, AmbiguousSymbolError("dup", candidates, 7))
    assert payload["candidateTotal"] == 7
    assert payload["candidates"] == [
        {"symbol": "sym-a", "dottedPath": "a.C.dup", "kind": "METHOD", "source": "scip"},
        {"symbol": "sym-b", "dottedPath": "b.D.dup", "kind": "METHOD", "source": "scip"},
    ]
    assert "ambiguous" in payload["error"]
    assert "a.C.dup" in payload["error"]  # leads with the qualifier hint


def test_go_to_definition_reports_resolved_symbol_for_a_bare_name():
    result = server.go_to_definition(REPO, "Greeter")
    assert result["symbol"] == "Greeter"
    assert result["resolvedSymbol"] == CLASS_SYMBOL
    assert result["definitions"]


def test_go_to_definition_omits_resolved_symbol_when_input_was_already_full():
    result = server.go_to_definition(REPO, CLASS_SYMBOL)
    assert "resolvedSymbol" not in result
    assert result["definitions"]


def test_find_references_reports_resolved_symbol_for_a_bare_name():
    result = server.find_references(REPO, "greet")
    assert result["resolvedSymbol"] == METHOD_SYMBOL


def test_unknown_symbol_returns_error_not_empty_references():
    result = server.find_references(REPO, "NoSuchSymbol")
    assert "no symbol named" in result["error"]
    assert "references" not in result


def test_search_coverage_fields_reports_complete_when_counts_match(monkeypatch, tmp_path):
    from jarvis import config, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
        registry.mark_tracked_files("myslug", 133)
    finally:
        registry.close()

    monkeypatch.setattr(server, "_zoekt_base_url_if_running", lambda: "http://x")
    monkeypatch.setattr(server, "zoekt_repo_documents", lambda url, repo: 133)

    assert server._search_coverage_fields("myslug") == {
        "searchCoverage": {"expected": 133, "indexed": 133, "complete": True}
    }


def test_search_coverage_fields_reports_incomplete_after_shard_loss(monkeypatch, tmp_path):
    """The incident: shards deleted after a successful index. Search kept
    answering with partial results and nothing reported a problem."""
    from jarvis import config, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
        registry.mark_tracked_files("myslug", 1307)
    finally:
        registry.close()

    monkeypatch.setattr(server, "_zoekt_base_url_if_running", lambda: "http://x")
    monkeypatch.setattr(server, "zoekt_repo_documents", lambda url, repo: 615)

    fields = server._search_coverage_fields("myslug")

    assert fields["searchCoverage"] == {"expected": 1307, "indexed": 615, "complete": False}


def test_search_coverage_fields_is_null_when_webserver_is_down(monkeypatch, tmp_path):
    from jarvis import config, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
        registry.mark_tracked_files("myslug", 133)
    finally:
        registry.close()

    monkeypatch.setattr(server, "_zoekt_base_url_if_running", lambda: None)

    fields = server._search_coverage_fields("myslug")

    assert fields["searchCoverage"] is None
    assert "not running" in fields["searchCoverageReason"]


def test_search_coverage_fields_is_null_when_never_recorded(monkeypatch, tmp_path):
    """Repos indexed before tracked_files existed have no expectation to
    compare against; report unknown rather than guessing."""
    from jarvis import config, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
    finally:
        registry.close()

    monkeypatch.setattr(server, "_zoekt_base_url_if_running", lambda: "http://x")

    fields = server._search_coverage_fields("myslug")

    assert fields["searchCoverage"] is None
    assert "reindex" in fields["searchCoverageReason"]


def test_search_coverage_fields_never_raises(monkeypatch, tmp_path):
    """A coverage probe failure must not replace a working status response
    with an error.

    `tracked_files` must be recorded first -- otherwise the function
    short-circuits at the "no tracked-file count recorded" branch before it
    ever reaches `_zoekt_base_url_if_running()`, and the test would pass
    vacuously without exercising the try/except at all. `call_count` makes
    that structurally impossible: the assertion below fails if the raiser
    was never invoked.
    """
    from jarvis import config, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("myslug", "/repos/mine", "python", "abc", "indexed")
        registry.mark_tracked_files("myslug", 133)
    finally:
        registry.close()

    call_count = 0

    def boom() -> str:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server, "_zoekt_base_url_if_running", boom)

    fields = server._search_coverage_fields("myslug")

    assert call_count == 1, "the raiser was never reached -- test is vacuous"
    assert fields["searchCoverage"] is None


def test_mcp_server_advertises_the_jarvis_name():
    """The FastMCP instance name is user-visible in `/mcp` output and in a
    client's server list, so it is part of the public surface, not an
    implementation detail."""
    from jarvis.server import mcp

    assert mcp.name == "jarvis"


# ---------------------------------------------------------------------------
# Per-file provider routing at the server/MCP boundary (Task 5, spec TSI-05)
# ---------------------------------------------------------------------------


def test_document_symbols_tool_reports_coverage_for_syntax_only_file(tmp_path: Path, monkeypatch):
    build_combined_snapshot(tmp_path, config.PROJECT, "combined-repo", config.BRANCH)
    result = server.document_symbols(repo="combined-repo", path="toy/util.py")
    assert "error" not in result
    assert [s["displayName"] for s in result["symbols"]] == ["helper"]
    assert result["symbols"][0]["source"] == "tree-sitter"
    assert result["coverage"]["state"] == "complete"


def test_document_symbols_tool_reports_error_and_coverage_for_unsupported_extension(
    tmp_path: Path, monkeypatch
):
    build_combined_snapshot(tmp_path, config.PROJECT, "combined-repo", config.BRANCH)
    result = server.document_symbols(repo="combined-repo", path="toy/notes.txt")
    assert "error" in result
    assert result["coverage"]["state"] == "unsupported"
    assert "symbols" not in result


def test_go_to_definition_tool_routes_a_syntax_identifier(tmp_path: Path, monkeypatch):
    build_combined_snapshot(tmp_path, config.PROJECT, "combined-repo", config.BRANCH)
    outline = server.document_symbols(repo="combined-repo", path="toy/util.py")
    syntax_id = outline["symbols"][0]["symbol"]
    result = server.go_to_definition(repo="combined-repo", symbol=syntax_id)
    assert "error" not in result
    assert "resolvedSymbol" not in result  # opaque id round-trips unchanged
    assert result["definitions"][0]["source"] == "tree-sitter"
    assert result["definitions"][0]["path"] == "toy/util.py"


def test_find_references_tool_renders_required_capability_for_a_syntax_identifier(
    tmp_path: Path, monkeypatch
):
    build_combined_snapshot(tmp_path, config.PROJECT, "combined-repo", config.BRANCH)
    outline = server.document_symbols(repo="combined-repo", path="toy/util.py")
    syntax_id = outline["symbols"][0]["symbol"]
    result = server.find_references(repo="combined-repo", symbol=syntax_id)
    assert result["requiredCapability"] == "findReferences"
    assert result["reason"]
    assert result["recovery"]
    assert "references" not in result


def test_call_hierarchy_tool_renders_required_capability_when_scip_absent(tmp_path: Path, monkeypatch):
    build_syntax_only_snapshot(tmp_path, config.PROJECT, "solo-repo", config.BRANCH)
    result = server.call_hierarchy(repo="solo-repo", symbol="solo")
    assert result["requiredCapability"] == "callHierarchy"
    assert "incomingCalls" not in result
    assert "outgoingCalls" not in result


def test_get_index_status_reports_tool_and_syntax_capabilities_for_a_combined_snapshot(
    tmp_path: Path, monkeypatch
):
    build_combined_snapshot(tmp_path, config.PROJECT, "combined-repo", config.BRANCH)
    result = server.get_index_status(repo="combined-repo")

    tools = result["capabilities"]["tools"]
    assert set(tools) == {"documentSymbols", "goToDefinition", "findReferences", "callHierarchy", "typeHierarchy"}
    for name in ("documentSymbols", "goToDefinition", "findReferences", "callHierarchy", "typeHierarchy"):
        assert tools[name]["available"] is True
        assert "providers" in tools[name] and "reason" in tools[name] and "recovery" in tools[name]
    assert "scip" in tools["documentSymbols"]["providers"]
    assert "tree-sitter" in tools["documentSymbols"]["providers"]
    assert tools["findReferences"]["providers"] == ["scip"]

    syntax_capability = result["capabilities"]["syntax"]
    assert syntax_capability["available"] is True
    assert syntax_capability["parsed"] >= 1
    assert syntax_capability["extractionIdentity"] is not None

    assert result["generation"] == "gen-combined"


def test_get_index_status_reports_missing_scip_tool_capabilities_for_a_syntax_only_snapshot(
    tmp_path: Path, monkeypatch
):
    build_syntax_only_snapshot(tmp_path, config.PROJECT, "solo-repo", config.BRANCH)
    result = server.get_index_status(repo="solo-repo")

    tools = result["capabilities"]["tools"]
    assert tools["findReferences"]["available"] is False
    assert tools["findReferences"]["providers"] == []
    assert tools["findReferences"]["reason"]
    assert tools["documentSymbols"]["available"] is True
    assert tools["documentSymbols"]["providers"] == ["tree-sitter"]

    assert result["capabilities"]["syntax"]["available"] is True


def test_get_index_status_existing_keys_unchanged_for_a_legacy_snapshot():
    """A legacy snapshot (this file's autouse `REPO` fixture) has no
    syntax tables at all -- `capabilities.tools`/`capabilities.syntax`
    must degrade honestly (no syntax providers) without disturbing any
    existing key."""
    result = server.get_index_status(repo=REPO)
    assert result["indexed"] is True
    assert result["capabilities"]["tools"]["documentSymbols"]["providers"] == ["scip"]
    assert result["capabilities"]["syntax"]["available"] is False


class _NotIndexedStub:
    """A QueryService whose repo has no published index."""

    def get_index_status(self, repo: str, repo_path: str | None = None):
        return False, None


def test_status_reports_starting_immediately_after_spawn(tmp_path, monkeypatch):
    """The regression that makes the whole poll contract usable: between
    Popen returning and the child registering there is no row and no lock, and
    a naive reading is 'not indexed, nothing running' -- so the agent spawns
    again, forever."""
    from jarvis import config, jobs, server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    jobs.write_launch_record("app", config.index_log("app"))

    class _Live:
        pid = 4242
        def poll(self):
            return None

    monkeypatch.setitem(server._launched, "app", _Live())
    monkeypatch.setattr(server, "_service", lambda: _NotIndexedStub())

    result = server.get_index_status(repo="app")
    assert result["indexed"] is False
    assert result["indexing"]["state"] == "starting"
    assert result["indexing"]["pid"] == 4242


def test_status_reports_failed_at_startup_for_an_unreaped_child(tmp_path, monkeypatch):
    """A real child that exited but was never waited on is a zombie whose pid
    still answers os.kill(pid, 0). Observation must reap instead. Cannot be
    caught with a mocked Popen.

    Reuses `_exited_but_unreaped_child` from `tests/test_jobs.py` (import it)
    rather than spinning on `poll()`: spinning reaps, and a subsequent
    `poll()` on the reaped handle returns 0 from swallowed ECHILD, so the
    exit code asserted below would be fabricated."""
    from jarvis import config, jobs, server
    from tests.test_jobs import _exited_but_unreaped_child

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    jobs.write_launch_record("app", config.index_log("app"))
    child = _exited_but_unreaped_child()
    monkeypatch.setitem(server._launched, "app", child)
    monkeypatch.setattr(server, "_service", lambda: _NotIndexedStub())

    try:
        result = server.get_index_status(repo="app")
        assert result["indexing"]["state"] == "failed-at-startup"
        assert result["indexing"]["exitCode"] == 3
        assert result["indexing"]["log"].endswith("index-app.log")
    finally:
        child.wait()


def test_status_reports_running_when_the_child_holds_the_lock(tmp_path, monkeypatch):
    """Child-ready-before-parent-returns: the child got there first. The
    launch record must be exactly what the parent wrote, proving there is no
    post-spawn mutation to interleave with."""
    from jarvis import config, jobs, server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    jobs.write_launch_record("app", config.index_log("app"))
    before = config.index_launchfile("app").read_bytes()
    monkeypatch.setattr(server, "_service", lambda: _NotIndexedStub())

    with jobs.build_lock("app"):
        result = server.get_index_status(repo="app")

    assert result["indexing"]["state"] == "running"
    assert config.index_launchfile("app").read_bytes() == before


def test_status_leftover_record_does_not_mask_later_states(tmp_path, monkeypatch):
    """Rows 2-4 precede the record, which is never deleted."""
    from jarvis import config, jobs, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    jobs.write_launch_record("app", config.index_log("app"))

    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("app", "/tmp/app", "python", None, "indexing")
    finally:
        registry.close()
    monkeypatch.setattr(server, "_service", lambda: _NotIndexedStub())
    assert server.get_index_status(repo="app")["indexing"]["state"] == "abandoned"

    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.mark_status("app", "failed")
    finally:
        registry.close()
    assert "indexing" not in server.get_index_status(repo="app")


def test_status_omits_indexing_when_nothing_in_flight(tmp_path, monkeypatch):
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server, "_service", lambda: _NotIndexedStub())
    assert "indexing" not in server.get_index_status(repo="app")


def test_index_repo_tool_spawns_and_reports_starting(tmp_path, monkeypatch):
    from jarvis import config, jobs, server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.index_cli_module(), "ensure_git_repo", lambda p: None)

    captured: dict = {}

    class _Fake:
        pid = 999

        def poll(self):
            return None

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Fake()

    monkeypatch.setattr(server.subprocess, "Popen", _fake_popen)

    result = server.index_repo_tool(path=str(repo))

    assert result["repo"] == "app"
    assert result["state"] == "starting"
    assert result["pid"] == 999
    assert captured["argv"][:5] == [
        "/fake/jarvis", "index", str(repo.resolve()), "--slug", "app",
    ]
    assert "--no-semantic" in captured["argv"]
    # The record exists by the time the tool returns, so an immediate poll
    # can never see "nothing running".
    assert jobs.read_launch_record("app") is not None


def test_index_repo_tool_never_inherits_server_stdout(tmp_path, monkeypatch):
    """stdio IS the MCP transport: one inherited write corrupts the JSON-RPC
    stream and kills the session."""
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.index_cli_module(), "ensure_git_repo", lambda p: None)

    captured: dict = {}

    class _Fake:
        pid = 1
        def poll(self):
            return None

    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda argv, **kw: (captured.update(kw), _Fake())[1])
    server.index_repo_tool(path=str(repo))

    assert captured["stdout"] is server.subprocess.DEVNULL
    assert captured["stdin"] is server.subprocess.DEVNULL
    assert captured["stderr"] is not None
    assert captured["stderr"] is not server.subprocess.DEVNULL
    assert captured["start_new_session"] is True
    assert captured["env"]["JARVIS_DATA_DIR"] == str(config.data_dir())


def test_index_repo_tool_semantic_true_omits_the_flag(tmp_path, monkeypatch):
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.index_cli_module(), "ensure_git_repo", lambda p: None)

    captured: dict = {}

    class _Fake:
        pid = 1
        def poll(self):
            return None

    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda argv, **kw: (captured.update(argv=argv), _Fake())[1])
    server.index_repo_tool(path=str(repo), semantic=True, scip=False)

    assert "--no-semantic" not in captured["argv"]
    assert "--no-scip" in captured["argv"]


def test_index_repo_tool_passes_a_registered_custom_slug(tmp_path, monkeypatch):
    """A repo registered under an explicit --slug resolves by path, not by
    basename. Without passing --slug through, the child derives the basename,
    trips `_reject_duplicate_slug_for_path`, and dies -- while this tool has
    already told the caller to poll the custom slug."""
    from jarvis import config, server
    from jarvis.registry import Registry

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    registry = Registry(config.data_dir() / "registry.db")
    try:
        registry.upsert("custom-name", str(repo), "python", None, "indexed")
    finally:
        registry.close()

    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.index_cli_module(), "ensure_git_repo", lambda p: None)

    captured: dict = {}

    class _Fake:
        pid = 7
        def poll(self):
            return None

    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda argv, **kw: (captured.update(argv=argv), _Fake())[1])
    result = server.index_repo_tool(path=str(repo))

    assert result["repo"] == "custom-name"
    assert result["reindex"] is True
    assert captured["argv"][3:5] == ["--slug", "custom-name"]


def test_index_repo_tool_does_not_spawn_a_second_child(tmp_path, monkeypatch):
    """The lock probe leaves a window: a child spawned moments ago may not
    hold the lock yet. Spawning again would drop the first handle, leaking a
    zombie that can no longer be reaped."""
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.index_cli_module(), "ensure_git_repo", lambda p: None)

    class _Live:
        pid = 11
        def poll(self):
            return None

    monkeypatch.setitem(server._launched, "app", _Live())

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn a duplicate")

    monkeypatch.setattr(server.subprocess, "Popen", _no_spawn)
    result = server.index_repo_tool(path=str(repo))

    assert result["alreadyRunning"] is True
    assert result["pid"] == 11


def test_index_repo_tool_preflight_missing_zoekt(tmp_path, monkeypatch):
    """TSI removed the language-toolchain blocker, not every binary: Stage 4
    Zoekt is still required, so say so instead of spawning a doomed child."""
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which",
                        lambda name: None if name == "zoekt-git-index" else f"/usr/bin/{name}")

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn")

    monkeypatch.setattr(server.subprocess, "Popen", _no_spawn)
    result = server.index_repo_tool(path=str(repo))

    assert "zoekt-git-index" in result["error"]
    assert result["recovery"] == "brew reinstall jarvis, then retry indexing"


def test_missing_jarvis_launcher_recommends_homebrew_reinstall(monkeypatch):
    """The launcher is bundled with the Homebrew package, not installed by
    setup.sh or the retired PyPI distribution."""
    from jarvis import server

    monkeypatch.setattr(server.sys, "executable", "/missing/python")
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)

    with pytest.raises(RuntimeError, match="brew reinstall jarvis"):
        server._jarvis_bin()


def test_index_repo_tool_preflight_not_a_git_repo(tmp_path, monkeypatch):
    from jarvis import index_cli, server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "plain"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")

    # Brief drift fix: `subprocess.run` inside `ensure_git_repo` constructs
    # the module-global `Popen`, so a blanket patch would make the git
    # pre-flight probe itself raise "must not spawn". Pass `git` through to
    # the real Popen (it IS the pre-flight working) and forbid only the
    # index child.
    real_popen = server.subprocess.Popen

    def _no_spawn(argv, *a, **k):
        if argv[0] == "git":
            return real_popen(argv, *a, **k)
        raise AssertionError("must not spawn")

    monkeypatch.setattr(server.subprocess, "Popen", _no_spawn)
    result = server.index_repo_tool(path=str(repo))
    assert "not a git repository" in result["error"]


def test_index_repo_tool_adopts_a_live_job(tmp_path, monkeypatch):
    from jarvis import jobs, server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    monkeypatch.setattr(server, "_jarvis_bin", lambda: "/fake/jarvis")
    monkeypatch.setattr(server.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(server.index_cli_module(), "ensure_git_repo", lambda p: None)

    def _no_spawn(*a, **k):
        raise AssertionError("must not spawn a duplicate")

    monkeypatch.setattr(server.subprocess, "Popen", _no_spawn)
    with jobs.build_lock("app"):
        result = server.index_repo_tool(path=str(repo))

    assert result["alreadyRunning"] is True
    assert result["state"] == "running"


def test_index_repo_tool_reports_missing_binary(tmp_path, monkeypatch):
    from jarvis import server

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "app"
    repo.mkdir()
    # Brief drift fix: the verbatim brief test patches only `shutil.which`,
    # but any installed venv ships `bin/jarvis` next to the interpreter, so
    # `_jarvis_bin()`'s candidate lookup would succeed and the tool would
    # report the zoekt error instead. Point sys.executable at a directory
    # with no `jarvis` sibling so "no jarvis anywhere" actually holds; the
    # test logic (real `_jarvis_bin`, PATH lookup dead) is unchanged.
    monkeypatch.setattr(server.sys, "executable",
                        str(tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    result = server.index_repo_tool(path=str(repo))
    assert "jarvis" in result["error"]


@pytest.mark.anyio
async def test_index_repo_tool_is_registered():
    """Extends the file's existing EXPECTED_TOOLS convention rather than
    reaching into FastMCP internals."""
    async with create_connected_server_and_client_session(server.mcp) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert listed == EXPECTED_TOOLS
