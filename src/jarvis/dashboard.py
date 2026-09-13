"""Localhost operator dashboard: a stdlib HTTP console over the registry,
jobs, and query seams, plus operator actions that spawn the same detached
index children as the MCP `indexRepo` tool.

Deliberately NOT: remote-accessible (binds 127.0.0.1 exclusively),
authenticated (single-tenant, same contract as the MCP server), or
framework-based (stdlib http.server; the frontend is framework-free).
The HTTP boundary mirrors server.py's never-raise contract: every broad
catch returns {"error": ...} JSON and logs to stderr.
"""

from __future__ import annotations

import concurrent.futures
import importlib.resources
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from jarvis import config

_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost"}
_LOG_CHUNK = 64_000
_ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


# Semantic search embeds its query with a native torch stack that does not
# tolerate concurrent first-use initialization: two simultaneous model loads
# in one process have segfaulted (observed on macOS/arm64). All embedding
# traffic is therefore serialized through a single worker, and callers wait
# at most _SEMANTIC_TIMEOUT seconds — a cold first load (model download)
# surfaces as a per-signal "timed out" degradation instead of hanging the
# response, and later calls hit the warm cache.
_SEMANTIC_TIMEOUT = 20.0
_semantic_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="semantic")


_semantic_gate = threading.Semaphore(1)


class DashboardError(Exception):
    """Handler-level failure carrying its HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _guarded_semantic(fn):
    """Run an embedding-touching callable on the serialized semantic worker.
    Two bounds: a non-blocking gate rejects a second query while one is in
    flight (503 — the single worker would otherwise bury fresh searches
    behind a backlog of abandoned ones), and the caller's wait is capped at
    `_SEMANTIC_TIMEOUT` (504 — the worker keeps loading, so a retry after a
    timeout hits the warm model cache)."""
    if not _semantic_gate.acquire(blocking=False):
        raise DashboardError(503, "semantic search is busy — an embedding query is already in flight; retry in a moment")

    def task():
        try:
            return fn()
        finally:
            _semantic_gate.release()

    try:
        future = _semantic_worker.submit(task)
    except BaseException:
        _semantic_gate.release()
        raise
    try:
        return future.result(timeout=_SEMANTIC_TIMEOUT)
    except concurrent.futures.TimeoutError as exc:
        raise DashboardError(504, "semantic search timed out — the embedding model is still loading; retry shortly") from exc


_TOOL_NAMES = {
    "documentSymbols": "document_symbols",
    "goToDefinition": "go_to_definition",
    "findReferences": "find_references",
    "callHierarchy": "call_hierarchy",
    "typeHierarchy": "type_hierarchy",
    "getIndexStatus": "get_index_status",
    "searchCode": "search_code",
    "semanticSearch": "semantic_search_tool",
    "blastRadius": "blast_radius_tool",
    "indexRepo": "index_repo_tool",
}




def assets_bytes(name: str) -> bytes:
    root = importlib.resources.files("jarvis").joinpath("dashboard_assets")
    return root.joinpath(name).read_bytes()


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("jarvis-mcp")
    except Exception:
        return "dev"


def _dir_bytes(path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def _storage_sizes(slug: str) -> dict[str, int]:
    scip = _dir_bytes(config.index_dir(slug))
    zoekt_dir = config.data_dir() / ".zoekt"
    zoekt = sum(
        f.stat().st_size
        for f in zoekt_dir.glob(f"{slug}_v*")
        if f.is_file()
    ) if zoekt_dir.exists() else 0
    lance = _dir_bytes(config.lancedb_dir() / f"{slug}.lance")
    return {"scip": scip, "zoekt": zoekt, "lance": lance, "total": scip + zoekt + lance}


def _graph_edges() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return graph nodes and dependent-to-dependency edges read-only."""
    import sqlite3

    db_path = config.data_dir() / "registry.db"
    if not db_path.exists():
        return [], []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            nodes = [
                {"id": package_id, "repo": repo, "name": name}
                for package_id, repo, name in conn.execute("SELECT id, repo, name FROM packages")
            ]

            edges = [
                {"from": from_package_id, "to": to_package_id}
                for from_package_id, to_package_id in conn.execute(
                    "SELECT from_package_id, to_package_id FROM edges"
                )
            ]
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return [], []
    finally:
        conn.close()
    return nodes, edges


def _registry_exists() -> bool:
    return (config.data_dir() / "registry.db").exists()


class DashboardApi:
    """Route table and handlers. dispatch() never raises: it maps
    DashboardError and unexpected exceptions onto (status, {"error": ...})."""

    def __init__(self) -> None:
        # Static routes, populated via _add by this task and Tasks 4-7:
        # path -> (handler, methods). Dynamic /api/repos/{slug}/... segments
        # resolve through _resolve_dynamic (Tasks 4-6).
        self._routes: dict[str, tuple[Any, frozenset[str]]] = {}
        self._add("/api/overview", frozenset({"GET"}), self._overview)
        self._add("/api/repos", frozenset({"GET"}), self._repos)
        self._add("/api/graph", frozenset({"GET"}), self._graph)
        self._add("/api/repos/index", frozenset({"POST"}), self._index_action)
        self._add("/api/search", frozenset({"GET"}), self._search)
        self._add("/api/tools", frozenset({"GET"}), self._tool_catalog)



    def _add(self, path: str, methods: frozenset[str], handler: Any) -> None:
        self._routes[path] = (handler, methods)

    def dispatch(self, method: str, path: str,
                 query: dict[str, list[str]], body: dict[str, Any],
                 ) -> tuple[int, dict[str, Any]]:
        try:
            handler, methods = self._route(path)
            if method not in methods:
                raise DashboardError(405, f"method {method} not allowed")
            return handler(query, body)
        except DashboardError as exc:
            return exc.status, {"error": exc.message}
        except Exception as exc:  # Broad on purpose: the HTTP boundary never raises.
            print(f"dashboard: {method} {path}: {exc!r}", file=sys.stderr)
            return 500, {"error": str(exc)}

    def _route(self, path: str) -> tuple[Any, frozenset[str]]:
        if path in self._routes:
            handler, methods = self._routes[path]
            return handler, methods
        resolved = self._resolve_dynamic(path)  # /api/repos/{slug}/... — Tasks 4-6
        if resolved is not None:
            return resolved
        raise DashboardError(404, f"not found: {path}")

    def _resolve_dynamic(self, path: str) -> tuple[Any, frozenset[str]] | None:
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "repos"]:
            slug = parts[2]
            if len(parts) == 3:
                return (
                    lambda query, body, repo_slug=slug: self._repo_detail(repo_slug, query, body),
                    frozenset({"GET"}),
                )
            if len(parts) == 4 and parts[3] == "log":
                return (lambda q, b, s=slug: self._log_tail(s, q, b), frozenset({"GET"}))
            if len(parts) == 4 and parts[3] == "file":
                return (lambda q, b, s=slug: self._source_slice(s, q, b), frozenset({"GET"}))
            if len(parts) == 4 and parts[3] == "reindex":
                return (lambda q, b, s=slug: self._reindex_action(s, q, b),
                        frozenset({"POST"}))
            if len(parts) == 4 and parts[3] == "forget":
                return (lambda q, b, s=slug: self._forget_action(s, q, b),
                        frozenset({"POST"}))

        if len(parts) == 4 and parts[:2] == ["api", "tools"] and parts[3] == "invoke":
            name = parts[2]
            return (
                lambda query, body, tool_name=name: self._tool_invoke(tool_name, query, body),
                frozenset({"POST"}),
            )


        return None

    def _server_module(self):
        from jarvis import server
        return server

    def _tool_catalog(self, query, body):
        import inspect

        server = self._server_module()
        tools = []
        for mcp_name, attr in _TOOL_NAMES.items():
            fn = getattr(server, attr)
            doc = (fn.__doc__ or "").strip().splitlines()[0]
            params = []
            signature = inspect.signature(fn)
            for pname, param in signature.parameters.items():
                annotation = param.annotation
                type_name = (getattr(annotation, "__name__", None)
                             or str(annotation).replace("typing.", "")
                             or "str")
                if pname in ("repo_path", "repo", "path", "symbol", "query",
                             "symbol_or_package"):
                    type_name = "str"
                params.append({"name": pname, "type": type_name,
                               "required": param.default is inspect.Parameter.empty,
                               "default": None if param.default is inspect.Parameter.empty
                               else param.default})
            tools.append({"name": mcp_name, "description": doc, "params": params})
        return 200, {"tools": tools}

    def _tool_invoke(self, name: str, query, body):
        import inspect
        import time

        server = self._server_module()
        attr = _TOOL_NAMES.get(name)
        if attr is None:
            raise DashboardError(404, f"no such tool: {name}")
        fn = getattr(server, attr)
        signature = inspect.signature(fn)
        kwargs = dict(body)
        for pname, param in signature.parameters.items():
            if param.default is inspect.Parameter.empty and pname not in kwargs:
                raise DashboardError(400, f"missing required parameter {pname!r}")
        kwargs = {k: v for k, v in kwargs.items() if k in signature.parameters}
        t0 = time.perf_counter()
        result = _guarded_semantic(lambda: fn(**kwargs)) if name == "semanticSearch" else fn(**kwargs)
        return 200, {"result": result,
                     "elapsedMs": round((time.perf_counter() - t0) * 1000, 1)}

    def _search(self, query, body):
        import time

        q = (query.get("q", [""])[0] or "").strip()
        if not q:
            raise DashboardError(400, "query parameter 'q' is required")
        repo = query.get("repo", [None])[0]
        server = self._server_module()
        out: dict[str, Any] = {"query": q, "repo": repo, "scoped": repo is not None,
                               "lexical": None, "semantic": None, "symbols": None}
        t0 = time.perf_counter()
        try:
            from jarvis.search import search_zoekt

            base = server._zoekt().ensure_running()
            result = search_zoekt(base, f"r:{repo} {q}" if repo else q)
            out["lexical"] = {
                "hits": [{"repo": h.repo, "path": h.path, "lineNumber": h.line_number,
                          "lineText": h.line_text} for h in result.hits],
                "total": result.total_matches,
                "truncated": result.total_matches > len(result.hits),
            }
        except Exception as exc:
            out["lexicalError"] = str(exc)
        if repo is not None:
            try:
                from jarvis import semantic

                out["semantic"] = _guarded_semantic(lambda: semantic.semantic_search(
                    repo, q, 10,
                    zoekt_base_url=server._zoekt_base_url_or_none(),
                    scip_conn=server._scip_conn_or_none(repo)))
            except Exception as exc:
                out["semanticError"] = str(exc)
            try:
                from jarvis.symbol_search import search_symbols

                conn = server._scip_conn_or_none(repo)
                if conn is not None:
                    out["symbols"] = [
                        {"path": h.file_path, "startLine": h.start_line,
                         "endLine": h.end_line, "name": h.dotted_path, "kind": h.kind}
                        for h in search_symbols(conn, q)
                    ]
            except Exception as exc:
                out["symbolsError"] = str(exc)
        out["elapsedMs"] = round((time.perf_counter() - t0) * 1000, 1)
        return 200, out

    def _overview(self, query, body):
        server = self._server_module()
        try:
            base = server._zoekt().base_url_if_running()
            zoekt_running = base is not None
        except Exception:
            base, zoekt_running = None, False
        data_dir = config.data_dir()
        disk = (
            _dir_bytes(data_dir / "scip")
            + _dir_bytes(data_dir / ".zoekt")
            + _dir_bytes(data_dir / "lancedb")
        )
        if _registry_exists():
            registry = self._registry()
            try:
                repos = len(registry.list())
            finally:
                registry.close()
        else:
            repos = 0
        return 200, {
            "version": _version(),
            "dataDir": str(data_dir),
            "zoektBase": base,
            "zoektRunning": zoekt_running,
            "repos": repos,
            "diskBytes": disk,
        }

    def _registry(self):
        from jarvis.registry import Registry
        return Registry(config.data_dir() / "registry.db")

    def _repo_row(self, entry) -> dict[str, Any]:
        from jarvis.registry import recovery_for

        server = self._server_module()
        freshness: dict[str, Any] | None = None
        indexing: dict[str, Any] | None = None
        indexed = False
        try:
            indexed, snapshot = server._service().get_index_status(entry.slug, entry.path)
            freshness = {
                "commit": snapshot.commit,
                "freshness": snapshot.freshness.value,
                "stale": snapshot.stale,
                "generation": snapshot.generation,
            }
        except Exception:
            pass
        try:
            fields = server._indexing_fields(entry.slug, indexed)
            indexing = fields.get("indexing")
        except Exception:
            pass
        return {
            "slug": entry.slug,
            "path": entry.path,
            "language": entry.language,
            "status": entry.status,
            "scipState": entry.scip_state,
            "scipEnabled": entry.scip_enabled,
            "semanticIndexedAt": entry.semantic_indexed_at.isoformat()
            if entry.semantic_indexed_at
            else None,
            "semanticDeclined": entry.semantic_declined,
            "lastIndexed": entry.last_indexed.isoformat(),
            "freshness": freshness,
            "indexing": indexing,
            "recovery": recovery_for(entry),
            "storageBytes": _storage_sizes(entry.slug),
        }

    def _repos(self, query, body):
        if not _registry_exists():
            return 200, {"repos": []}
        registry = self._registry()
        try:
            entries = registry.list()
        finally:
            registry.close()
        return 200, {"repos": [self._repo_row(entry) for entry in entries]}

    def _repo_entry_or_404(self, slug: str):
        if not _registry_exists():
            raise DashboardError(404, f"no such repo: {slug}")
        registry = self._registry()
        try:
            entry = registry.get(slug)
        finally:
            registry.close()
        if entry is None:
            raise DashboardError(404, f"no such repo: {slug}")
        return entry

    def _repo_detail(self, slug: str, query, body):
        from jarvis.registry import recovery_for

        entry = self._repo_entry_or_404(slug)
        server = self._server_module()
        row = self._repo_row(entry)
        index_dir = config.index_dir(slug)
        try:
            current = (index_dir / "current").read_text().strip()
        except OSError:
            current = None
        snapshots = [
            {
                "name": snapshot.name,
                "bytes": snapshot.stat().st_size,
                "mtime": snapshot.stat().st_mtime,
                "current": snapshot.name == current,
            }
            for snapshot in sorted(
                index_dir.glob("index-*.db"), key=lambda candidate: -candidate.stat().st_mtime
            )
        ] if index_dir.exists() else []
        # `get_index_status` returns the full tool envelope; the renderer
        # wants the inner `capabilities` object plus `last_index_run`.
        status_payload = server.get_index_status(slug, entry.path)
        capabilities = status_payload.get("capabilities") or {}
        last_run = status_payload.get("last_index_run") or {}
        nodes, edges = _graph_edges()
        own_ids = {node["id"] for node in nodes if node["repo"] == slug}
        by_id = {node["id"]: f"{node['repo']}:{node['name']}" for node in nodes}
        depends_on = sorted({by_id[edge["to"]] for edge in edges if edge["from"] in own_ids})
        depended_on_by = sorted(
            {by_id[edge["from"]] for edge in edges if edge["to"] in own_ids}
        )
        try:
            log_path = str(config.index_log(slug))
        except Exception:
            log_path = None
        row.update({
            "snapshots": snapshots,
            "capabilities": capabilities,
            "last_index_run": last_run,
            "graph": {"dependsOn": depends_on, "dependedOnBy": depended_on_by},
            "logPath": log_path,
            "recovery": recovery_for(entry),
        })
        return 200, row

    def _log_tail(self, slug: str, query, body):
        self._repo_entry_or_404(slug)
        log = config.index_log(slug)
        try:
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            raise DashboardError(400, "offset must be an integer")
        if offset < 0:
            offset = 0
        try:
            size = log.stat().st_size
        except OSError:
            return 200, {"chunk": "", "nextOffset": 0, "size": 0}
        with log.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read(_LOG_CHUNK).decode(errors="replace")
        return 200, {
            "chunk": chunk,
            "nextOffset": min(offset + len(chunk.encode()), size),
            "size": size,
        }

    def _source_slice(self, slug: str, query, body):
        entry = self._repo_entry_or_404(slug)
        rel = query.get("p", [""])[0]
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise DashboardError(400, f"invalid path {rel!r}")
        root = Path(entry.path).resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise DashboardError(400, f"path escapes repo root: {rel}")
        if not target.is_file():
            raise DashboardError(404, f"no such file: {rel}")
        try:
            start = int(query.get("start", ["1"])[0])
            end = int(query.get("end", [str(start + 200)])[0])
        except ValueError:
            raise DashboardError(400, "start/end must be integers")
        with target.open(errors="replace") as fh:
            lines = fh.readlines()
        selected = [ln.rstrip("\n") for ln in lines[max(start - 1, 0):max(end, 0)]]
        return 200, {"path": rel, "start": start, "end": end, "lines": selected}

    def _spawn_or_error(self, path: str, semantic: bool, scip: bool | None):
        server = self._server_module()
        payload = server._spawn_index(path, semantic=semantic, scip=scip)
        if "error" in payload:
            raise DashboardError(400, payload["error"])
        if payload.get("alreadyRunning"):
            payload.pop("alreadyRunning")
            return 409, payload
        return 202, payload

    def _index_action(self, query, body):
        path = body.get("path")
        if not path or not isinstance(path, str):
            raise DashboardError(400, "body must include a string 'path'")
        semantic = bool(body.get("semantic", False))
        scip = body.get("scip") if body.get("scip") is None else bool(body["scip"])
        return self._spawn_or_error(path, semantic, scip)

    def _reindex_action(self, slug: str, query, body):
        entry = self._repo_entry_or_404(slug)
        return self._spawn_or_error(entry.path, bool(body.get("semantic", False)),
                                    body.get("scip"))

    def _forget_action(self, slug: str, query, body):
        self._repo_entry_or_404(slug)
        if body.get("confirm") != slug:
            raise DashboardError(400, f"confirm must be the exact slug {slug!r}")
        from jarvis import index_cli
        ok, message = index_cli.forget_repo(slug)
        if not ok:
            status = 409 if "lock" in message.lower() else 400
            raise DashboardError(status, message)
        return 200, {"ok": True, "message": message}

    def _graph(self, query, body):
        nodes, edges = _graph_edges()
        return 200, {"nodes": nodes, "edges": edges}


def make_handler(api: DashboardApi) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet; errors still stderr
            pass

        # -- guard ---------------------------------------------------------
        def _guard(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in _ALLOWED_HOSTNAMES:
                self._send(403, {"error": f"forbidden Host {host!r}"})
                return False
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin is not None:
                    try:
                        origin_host = urlparse(origin).hostname
                    except ValueError:
                        origin_host = None  # unparseable origin: fail closed
                    if origin_host not in _ALLOWED_HOSTNAMES:
                        self._send(403, {"error": f"forbidden Origin {origin!r}"})
                        return False
            return True

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            blob = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _serve_static(self, name: str) -> None:
            try:
                blob = assets_bytes(name)
            except FileNotFoundError:
                self._send(404, {"error": f"no asset {name}"})
                return
            ctype = _ASSET_TYPES.get("." + name.rsplit(".", 1)[-1], "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _dispatch(self, body: dict[str, Any]) -> None:
            if not self._guard():
                return
            try:
                parsed = urlparse(self.path)
            except ValueError:
                self._send(400, {"error": "invalid request path"})
                return
            if parsed.path == "/" or parsed.path == "/index.html":
                self._serve_static("index.html")
                return
            if not parsed.path.startswith("/api/"):
                name = parsed.path.lstrip("/")
                if name in {"app.js", "style.css"}:
                    self._serve_static(name)
                    return
                self._send(404, {"error": f"not found: {parsed.path}"})
                return
            query = parse_qs(parsed.query)
            status, payload = api.dispatch(self.command, unquote(parsed.path), query, body)
            self._send(status, payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._dispatch({})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"error": "invalid Content-Length"})
                return
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "request body is not valid JSON"})
                return
            self._dispatch(body)

    return DashboardHandler


def serve(port: int | None = None, *, open_browser: bool = True) -> None:
    """Block serving the dashboard on 127.0.0.1. Ctrl-C shuts down cleanly."""
    resolved = port if port is not None else config.dashboard_port()
    api = DashboardApi()
    handler = make_handler(api)
    httpd = ThreadingHTTPServer(("127.0.0.1", resolved), handler)
    url = f"http://127.0.0.1:{resolved}"
    print(f"jarvis dashboard listening on {url} (Ctrl-C to stop)", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
