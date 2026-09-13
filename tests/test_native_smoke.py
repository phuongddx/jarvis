"""Unit tests for native smoke-test helper seams."""

from __future__ import annotations

import importlib.util
import json
import signal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "native_smoke.py"
spec = importlib.util.spec_from_file_location("native_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_jsonrpc_line_is_single_line_json():
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
    encoded = smoke.jsonrpc_line(payload)
    assert encoded.endswith("\n")
    assert json.loads(encoded) == payload


def test_semantic_failure_detects_tool_error():
    response = {
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"error": "semantic unavailable"})}
            ],
            "isError": True,
        }
    }
    assert smoke.semantic_unavailable(response) is True


def test_semantic_failure_detects_error_envelope():
    response = {
        "result": {
            "content": [
                {"type": "text", "text": json.dumps({"error": "semantic unavailable"})}
            ]
        }
    }
    assert smoke.semantic_unavailable(response) is True


def test_successful_response_is_not_semantic_failure():
    response = {
        "result": {
            "content": [{"type": "text", "text": json.dumps({"results": []})}]
        }
    }
    assert smoke.semantic_unavailable(response) is False


def test_semantic_assertion_rejects_uv_install_hint():
    response = {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "error": (
                                "semantic search is not included in the "
                                "Homebrew binary distribution; install with uv"
                            )
                        }
                    ),
                }
            ]
        }
    }

    with pytest.raises(RuntimeError, match="(?i)uv"):
        smoke.assert_semantic_unavailable(response)


def test_exact_version_assertion_rejects_unknown_and_mismatch():
    smoke.assert_exact_version("jarvis 0.10.0\n", "0.10.0")

    for output in ("jarvis unknown\n", "jarvis 0.9.9\n"):
        with pytest.raises(RuntimeError, match="unexpected version"):
            smoke.assert_exact_version(output, "0.10.0")


def test_help_assertion_rejects_empty_output():
    smoke.assert_help_output("usage: jarvis [-h]\n")

    with pytest.raises(RuntimeError, match="unexpected help output"):
        smoke.assert_help_output("")


def test_dashboard_asset_assertion_checks_response_shape():
    smoke.assert_dashboard_asset(
        "/",
        200,
        "text/html; charset=utf-8",
        "<!doctype html><html></html>",
    )
    smoke.assert_dashboard_asset(
        "/style.css",
        200,
        "text/css; charset=utf-8",
        "body { color: red }",
    )

    with pytest.raises(RuntimeError, match="/ returned HTTP 500"):
        smoke.assert_dashboard_asset("/", 500, "text/html", "<html></html>")
    with pytest.raises(RuntimeError, match="/ has content type"):
        smoke.assert_dashboard_asset("/", 200, "text/plain", "<html></html>")
    with pytest.raises(RuntimeError, match="/ returned an empty body"):
        smoke.assert_dashboard_asset("/", 200, "text/html", "")


def test_cli_passes_exact_expected_version_to_smoke(monkeypatch, tmp_path):
    calls = []

    def fake_run_smoke(root, repo, data_dir, expected_version):
        calls.append((root, repo, data_dir, expected_version))

    monkeypatch.setattr(smoke, "run_smoke", fake_run_smoke)

    result = smoke.main(
        [
            "--root", str(tmp_path / "root"),
            "--repo", str(tmp_path / "repo"),
            "--data-dir", str(tmp_path / "data"),
            "--expected-version", "0.10.0",
        ]
    )

    assert result == 0
    assert calls == [
        (tmp_path / "root", tmp_path / "repo", tmp_path / "data", "0.10.0")
    ]


def test_dashboard_cleanup_terminates_then_kills_the_process_group():
    events: list[str] = []

    class Process:
        pid = 7321
        exited = False
        killed = False

        def poll(self):
            return 15 if self.exited else None

        def wait(self, timeout=None):
            if self.exited:
                return 15
            events.append("wait")
            self.exited = self.killed
            if not self.exited:
                raise smoke.subprocess.TimeoutExpired(
                    cmd="jarvis dashboard", timeout=timeout
                )
            return 15

        def kill(self):
            self.killed = True

    process = Process()

    def signal_group(target, number):
        assert target is process
        events.append(f"signal:{number.name}")

    smoke.stop_dashboard_process(process, signal_group=signal_group)

    assert events == ["signal:SIGTERM", "wait", "signal:SIGKILL", "wait"]


def test_mcp_close_closes_stdin_before_terminating_process_group():
    client = object.__new__(smoke.McpStdioClient)
    events: list[str] = []

    class StandardInput:
        def close(self):
            events.append("stdin-close")

    class Output:
        def close(self):
            events.append("stream-close")

    class Process:
        pid = 4321
        exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            self.exited = True
            return 0

        def terminate(self):
            events.append("direct-terminate")

        def kill(self):
            events.append("direct-kill")

    class Reader:
        def join(self, timeout=None):
            return None

    process = Process()
    client.process = process
    client.process.stdin = StandardInput()
    client.process.stdout = Output()
    client.process.stderr = Output()
    client._stdout_thread = Reader()
    client._stderr_thread = Reader()
    client._closed = False

    def signal_group(target: object, number: signal.Signals) -> None:
        assert target is process
        events.append(f"group-signal:{number.name}")

    client._signal_process_group = signal_group

    client.close()

    assert events == [
        "stdin-close",
        "wait:5.0",
        "group-signal:SIGTERM",
        "stream-close",
        "stream-close",
    ]


def test_native_environment_keeps_discovered_scip_python_directory(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(
        smoke.shutil,
        "which",
        lambda name: "/opt/homebrew/bin/scip-python"
        if name == "scip-python"
        else None,
    )
    env = smoke.native_environment(tmp_path, tmp_path / "data")
    assert env["PATH"].split(":")[:2] == [
        str(tmp_path / "bin"),
        "/opt/homebrew/bin",
    ]


def test_native_environment_pins_a_free_zoekt_port(monkeypatch, tmp_path):
    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def bind(self, address):
            return None

        def getsockname(self):
            return ("127.0.0.1", 49152)

    monkeypatch.setattr(smoke.socket, "socket", lambda: Socket())
    env = smoke.native_environment(tmp_path, tmp_path / "data")
    assert env["JARVIS_ZOEKT_PORT"] == "49152"
