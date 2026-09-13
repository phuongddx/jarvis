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
