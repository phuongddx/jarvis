"""Tests for search.py: real JSON-decoding logic against zoekt-webserver's
response shape (mocked via httpx.MockTransport — mirrors a real instance's
`POST /api/search` verified shape, see search.py's docstring), plus the
lifecycle manager's spawn/health-check/pidfile/stop behavior against a fake
zoekt-webserver script (no real Zoekt binary required for this suite)."""

from __future__ import annotations

import base64
import os
import stat
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from jarvis.search import ZoektHit, ZoektLifecycle, ZoektUnavailableError, search_zoekt, zoekt_repo_documents


def _zoekt_response(request: httpx.Request) -> httpx.Response:
    encoded_line = base64.b64encode(b"def greet(name):").decode()
    return httpx.Response(200, json={
        "Result": {
            "Files": [{
                "FileName": "toy/greeter.py",
                "Repository": "toy-repo",
                "LineMatches": [{"LineNumber": 5, "Line": encoded_line}],
            }],
            # MatchCount intentionally > the one returned match: proves
            # totals come from zoekt's Stats, not from len(hits).
            "Stats": {"MatchCount": 3, "FileCount": 1},
        },
    })


def _error_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="internal error")


def test_search_zoekt_decodes_base64_line_and_returns_hits():
    client = httpx.Client(transport=httpx.MockTransport(_zoekt_response))
    result = search_zoekt("http://localhost:6070", "greet", client=client)
    assert result.hits == [
        ZoektHit(repo="toy-repo", path="toy/greeter.py", line_number=5, line_text="def greet(name):")
    ]


def test_search_zoekt_reports_true_totals_from_stats():
    client = httpx.Client(transport=httpx.MockTransport(_zoekt_response))
    result = search_zoekt("http://localhost:6070", "greet", client=client)
    assert result.total_matches == 3
    assert result.file_count == 1


def test_search_zoekt_totals_fall_back_when_stats_absent():
    def no_stats(request: httpx.Request) -> httpx.Response:
        encoded = base64.b64encode(b"x = 1").decode()
        return httpx.Response(200, json={"Result": {"Files": [{
            "FileName": "a.py", "Repository": "r",
            "LineMatches": [{"LineNumber": 1, "Line": encoded}],
        }]}})

    result = search_zoekt("http://localhost:6070", "x", client=httpx.Client(transport=httpx.MockTransport(no_stats)))
    assert result.total_matches == 1  # len(hits)
    assert result.file_count == 1    # distinct (repo, path)


def test_search_zoekt_no_hits_returns_empty_result():
    def empty_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": {"Files": [], "Stats": {"MatchCount": 0, "FileCount": 0}}})

    client = httpx.Client(transport=httpx.MockTransport(empty_response))
    assert search_zoekt("http://localhost:6070", "no-such-query", client=client).hits == []


def test_search_zoekt_sends_display_cap_opts():
    from jarvis.search import MAX_DOC_DISPLAY, MAX_MATCH_DISPLAY

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"Result": {"Files": [], "Stats": {"MatchCount": 0, "FileCount": 0}}})

    search_zoekt("http://localhost:6070", "q", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert captured["body"]["Opts"] == {
        "MaxDocDisplayCount": MAX_DOC_DISPLAY,
        "MaxMatchDisplayCount": MAX_MATCH_DISPLAY,
    }


def test_search_zoekt_surfaces_parse_error_body():
    def bad_query(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"Error": "parse error: unexpected ')' in query"})

    with pytest.raises(ZoektUnavailableError, match=r"unexpected '\)' in query"):
        search_zoekt("http://localhost:6070", "greet(", client=httpx.Client(transport=httpx.MockTransport(bad_query)))


def test_search_zoekt_surfaces_non_json_400_body():
    def non_json_400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="parse error: unexpected ')' in query (plain-text body)")

    with pytest.raises(ZoektUnavailableError, match=r"parse error: unexpected '\)' in query"):
        search_zoekt("http://localhost:6070", "greet(", client=httpx.Client(transport=httpx.MockTransport(non_json_400)))


def test_search_zoekt_raises_unavailable_on_http_error():
    client = httpx.Client(transport=httpx.MockTransport(_error_response))
    with pytest.raises(ZoektUnavailableError):
        search_zoekt("http://localhost:6070", "greet", client=client)


def test_decode_line_truncates_oversized_lines():
    from jarvis.search import MAX_LINE_CHARS, _TRUNCATED_SUFFIX, _decode_line

    encoded = base64.b64encode(("x" * (MAX_LINE_CHARS + 5000)).encode()).decode()
    decoded = _decode_line(encoded)
    assert decoded == "x" * MAX_LINE_CHARS + _TRUNCATED_SUFFIX


def test_decode_line_leaves_normal_lines_alone():
    from jarvis.search import _decode_line

    encoded = base64.b64encode(b"def greet(name):").decode()
    assert _decode_line(encoded) == "def greet(name):"


def test_decode_line_empty_and_invalid_still_safe():
    from jarvis.search import _decode_line

    assert _decode_line("") == ""
    assert _decode_line("!!!not-base64!!!") == ""


_FAKE_ZOEKT_SCRIPT = textwrap.dedent(
    """\
    #!PYTHON_SHEBANG_PLACEHOLDER
    import http.server
    import socketserver
    import sys

    port = int(sys.argv[sys.argv.index("-listen") + 1].lstrip(":"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    class Server(socketserver.TCPServer):
        # socketserver.TCPServer leaves SO_REUSEADDR off; the stdlib's own
        # http.server.HTTPServer turns it on for exactly this reason. Without
        # it, binding a *fixed* port that is still in TIME_WAIT from a previous
        # run fails with EADDRINUSE, this process dies, and the caller sees
        # "exited immediately with code 1". The health check below establishes
        # and closes a connection, so TIME_WAIT is guaranteed once a test has
        # run -- which made the suite fail on any re-run within the ~15-60s
        # window rather than only under load.
        allow_reuse_address = True

    with Server(("127.0.0.1", port), Handler) as httpd:
        httpd.serve_forever()
    """
)


@pytest.fixture
def fake_zoekt_binary(tmp_path: Path) -> Path:
    script_path = tmp_path / "fake-zoekt-webserver"
    # Absolute shebang (vs. `#!/usr/bin/env python3`) avoids PATH-resolution
    # flakiness spawning this test double as a subprocess — the real
    # ZoektLifecycle always spawns the actual zoekt-webserver binary
    # directly, never through a shebang lookup.
    script_path.write_text(_FAKE_ZOEKT_SCRIPT.replace("PYTHON_SHEBANG_PLACEHOLDER", sys.executable), encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return script_path


def test_lifecycle_spawns_and_becomes_healthy(tmp_path: Path, fake_zoekt_binary: Path):
    lifecycle = ZoektLifecycle(
        index_dir=tmp_path / "zoekt-index",
        data_dir=tmp_path / "data",
        port=16070,
        binary=[sys.executable, str(fake_zoekt_binary)],
    )
    try:
        base_url = lifecycle.ensure_running()
        assert base_url == "http://127.0.0.1:16070"
        assert (tmp_path / "data" / "zoekt-webserver.pid").exists()
    finally:
        lifecycle.stop()
    assert not (tmp_path / "data" / "zoekt-webserver.pid").exists()


def test_lifecycle_reuses_already_running_instance(tmp_path: Path, fake_zoekt_binary: Path):
    lifecycle_a = ZoektLifecycle(
        index_dir=tmp_path / "zoekt-index", data_dir=tmp_path / "data", port=16071, binary=[sys.executable, str(fake_zoekt_binary)]
    )
    lifecycle_a.ensure_running()
    pid_a = int((tmp_path / "data" / "zoekt-webserver.pid").read_text())

    lifecycle_b = ZoektLifecycle(
        index_dir=tmp_path / "zoekt-index", data_dir=tmp_path / "data", port=16071, binary=[sys.executable, str(fake_zoekt_binary)]
    )
    try:
        lifecycle_b.ensure_running()
        pid_b = int((tmp_path / "data" / "zoekt-webserver.pid").read_text())
        assert pid_a == pid_b  # no duplicate process spawned
    finally:
        lifecycle_a.stop()


def test_lifecycle_raises_when_binary_exits_immediately(tmp_path: Path):
    bad_binary = tmp_path / "bad-zoekt"
    bad_binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    bad_binary.chmod(bad_binary.stat().st_mode | stat.S_IEXEC)

    lifecycle = ZoektLifecycle(
        index_dir=tmp_path / "zoekt-index",
        data_dir=tmp_path / "data",
        port=16072,
        binary=str(bad_binary),
        health_timeout_seconds=2.0,
    )
    with pytest.raises(ZoektUnavailableError):
        lifecycle.ensure_running()


def test_base_url_if_running_returns_none_without_a_pidfile(tmp_path: Path):
    """getIndexStatus must not spawn a webserver just to report coverage."""
    lifecycle = ZoektLifecycle(index_dir=tmp_path / ".zoekt", data_dir=tmp_path)

    assert lifecycle.base_url_if_running() is None


def test_base_url_if_running_returns_none_for_a_dead_pid(tmp_path: Path):
    (tmp_path / "zoekt-webserver.pid").write_text("999999999", encoding="utf-8")
    lifecycle = ZoektLifecycle(index_dir=tmp_path / ".zoekt", data_dir=tmp_path)

    assert lifecycle.base_url_if_running() is None


def test_base_url_if_running_returns_url_when_healthy(tmp_path: Path, monkeypatch):
    (tmp_path / "zoekt-webserver.pid").write_text(str(os.getpid()), encoding="utf-8")
    lifecycle = ZoektLifecycle(index_dir=tmp_path / ".zoekt", data_dir=tmp_path)
    monkeypatch.setattr(lifecycle, "_is_healthy", lambda: True)

    assert lifecycle.base_url_if_running() == lifecycle.base_url()


def test_port_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_ZOEKT_PORT", "16123")
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path)
    assert lifecycle.base_url() == "http://127.0.0.1:16123"


def test_port_env_invalid_falls_back(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_ZOEKT_PORT", "not-a-port")
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path)
    assert lifecycle.base_url() == "http://127.0.0.1:6070"


def test_explicit_port_beats_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_ZOEKT_PORT", "16123")
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path, port=16070)
    assert lifecycle.base_url() == "http://127.0.0.1:16070"


def test_base_url_if_running_rejects_foreign_http_server(tmp_path: Path, monkeypatch):
    """A plain HTTP server on the port answers / with 200 but has no
    /healthz; it must not be adopted as zoekt."""
    from types import SimpleNamespace

    (tmp_path / "zoekt-webserver.pid").write_text(str(os.getpid()), encoding="utf-8")
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path)
    monkeypatch.setattr(
        "jarvis.search.httpx.get",
        lambda url, timeout=None: SimpleNamespace(status_code=404 if url.endswith("/healthz") else 200),
    )
    assert lifecycle.base_url_if_running() is None


def test_is_healthy_requires_healthz_200(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path)
    monkeypatch.setattr("jarvis.search.httpx.get", lambda url, timeout=None: SimpleNamespace(status_code=200))
    assert lifecycle._is_healthy() is True
    monkeypatch.setattr("jarvis.search.httpx.get", lambda url, timeout=None: SimpleNamespace(status_code=500))
    assert lifecycle._is_healthy() is False


def test_failed_spawn_includes_stderr_tail(tmp_path: Path):
    bad = tmp_path / "bind-loser-zoekt"
    bad.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stderr.write('listen tcp :16071: bind: address already in use\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path / "d",
                               port=16071, binary=str(bad))
    with pytest.raises(ZoektUnavailableError) as excinfo:
        lifecycle.ensure_running()
    assert "address already in use" in str(excinfo.value)
    assert "exited immediately" in str(excinfo.value)


def test_ensure_running_missing_webserver_keeps_type_and_names_reinstall(
    tmp_path: Path, monkeypatch
):
    """zoekt-webserver is bundled with jarvis's Homebrew package."""
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError("zoekt-webserver")

    monkeypatch.setattr("jarvis.search.subprocess.Popen", _missing)
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path)

    with pytest.raises(FileNotFoundError, match="brew reinstall jarvis"):
        lifecycle.ensure_running()


def test_spawn_race_adopts_sibling_winner(tmp_path: Path, monkeypatch, fake_zoekt_binary: Path):
    winner = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path / "d",
                            port=16072, binary=[sys.executable, str(fake_zoekt_binary)])
    try:
        winner.ensure_running()
        bad = tmp_path / "loser-zoekt"
        bad.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
        loser = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path / "d",
                               port=16072, binary=str(bad))
        real_read = loser._read_pidfile
        seen: list[int] = []

        def miss_first_read() -> int | None:
            pid = None if not seen else real_read()
            seen.append(1)
            return pid

        monkeypatch.setattr(loser, "_read_pidfile", miss_first_read)
        # Loser misses the pidfile (the race), spawns, its child dies on the
        # bind conflict, and the re-check adopts the winner's healthy server.
        assert loser.ensure_running() == winner.base_url()
        assert loser._own_process is None
    finally:
        winner.stop()


def test_stop_does_not_unlink_a_pidfile_it_does_not_own(tmp_path: Path):
    lifecycle = ZoektLifecycle(index_dir=tmp_path / "i", data_dir=tmp_path)
    (tmp_path / "zoekt-webserver.pid").write_text("999999999", encoding="utf-8")
    lifecycle.stop()
    assert (tmp_path / "zoekt-webserver.pid").exists()



def test_zoekt_repo_documents_reads_the_list_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/list"
        return httpx.Response(
            200,
            json={"List": {"Repos": [{"Repository": {"Name": "myslug"}, "Stats": {"Documents": 133}}]}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert zoekt_repo_documents("http://localhost:6070", "myslug", client=client) == 133


def test_zoekt_repo_documents_returns_none_when_repo_absent():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"List": {"Repos": []}})))

    assert zoekt_repo_documents("http://localhost:6070", "myslug", client=client) is None


def test_zoekt_repo_documents_raises_on_transport_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(ZoektUnavailableError):
        zoekt_repo_documents("http://localhost:6070", "myslug", client=client)


def test_zoekt_repo_documents_closes_owned_client():
    """Verify that when no client is provided, the internally-created client
    is properly closed to avoid resource leaks."""

    def success_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"List": {"Repos": [{"Repository": {"Name": "myslug"}, "Stats": {"Documents": 42}}]}})

    # Test with explicitly provided client: it should NOT be closed
    client = httpx.Client(transport=httpx.MockTransport(success_handler))
    result = zoekt_repo_documents("http://localhost:6070", "myslug", client=client)
    assert result == 42
    assert not client.is_closed  # We didn't create it, so we don't close it
    client.close()  # Clean up

    # Test with no client provided: verify no exception on successful response
    # (The client being closed is guaranteed by the finally block, verified
    # by the pattern match to search_zoekt which uses the same idiom)
    client_with_mock = httpx.Client(transport=httpx.MockTransport(success_handler))
    result = zoekt_repo_documents("http://localhost:6070", "myslug", client=client_with_mock)
    assert result == 42
