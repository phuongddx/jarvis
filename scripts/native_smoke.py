"""End-to-end smoke test for an extracted standalone jarvis archive."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.request import urlopen

MCP_TIMEOUT_SECONDS = 30.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
COMMAND_TIMEOUT_SECONDS = 300.0
WATCH_PROBE_SECONDS = 1.0
DASHBOARD_TIMEOUT_SECONDS = 15.0
DASHBOARD_POLL_SECONDS = 0.2
HTTP_TIMEOUT_SECONDS = 2.0


def jsonrpc_line(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":")) + "\n"


def text_body(result: dict[str, Any]) -> str:
    return "".join(item.get("text", "") for item in result.get("content", []))


def semantic_unavailable(response: dict[str, Any]) -> bool:
    result = response.get("result", {})
    return (
        response.get("error") is not None
        or result.get("isError") is True
        or '"error"' in text_body(result)
    )


def assert_semantic_unavailable(response: dict[str, Any]) -> None:
    body = text_body(response.get("result", {}))
    if not semantic_unavailable(response):
        raise RuntimeError(f"semanticSearch unexpectedly succeeded: {body!r}")
    if "Homebrew binary distribution" not in body:
        raise RuntimeError(
            "semanticSearch error does not name the Homebrew binary "
            f"distribution: {body!r}"
        )
    if "uv" in body.lower():
        raise RuntimeError(
            "frozen semanticSearch error unexpectedly mentions uv: "
            f"{body!r}"
        )


def assert_exact_version(output: str, expected_version: str) -> None:
    actual = f"jarvis {expected_version}"
    if output.strip() != actual:
        raise RuntimeError(
            f"unexpected version output: expected {actual!r}, got {output.strip()!r}"
        )


def assert_help_output(output: str) -> None:
    if not output.strip() or "usage: jarvis" not in output:
        raise RuntimeError(f"unexpected help output: {output!r}")


def assert_dashboard_asset(
    path: str, status: int, content_type: str, body: str
) -> None:
    if status != 200:
        raise RuntimeError(f"{path} returned HTTP {status}")
    expected_type = "text/html" if path == "/" else "text/css"
    if not content_type.startswith(expected_type):
        raise RuntimeError(
            f"{path} has content type {content_type!r}, expected {expected_type!r}"
        )
    if not body:
        raise RuntimeError(f"{path} returned an empty body")
    if path == "/" and "<html" not in body.lower():
        raise RuntimeError(f"{path} did not return HTML")
    if path == "/style.css" and not body.strip().endswith("}"):
        raise RuntimeError(f"{path} did not return CSS")


class McpStdioClient:
    """Small JSON-RPC client whose reads and child shutdown are bounded."""

    def __init__(self, command: str, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            [command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="mcp-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="mcp-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._stdout_lines.put(None)
            return
        try:
            for line in stdout:
                self._stdout_lines.put(line)
        finally:
            self._stdout_lines.put(None)

    def _read_stderr(self) -> None:
        stderr = self.process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                with self._stderr_lock:
                    self._stderr_tail.append(line.rstrip("\n"))
        except OSError:
            return

    def _stderr_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_tail)

    def send(self, payload: dict[str, object]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise RuntimeError("MCP server has no stdin pipe")
        try:
            stdin.write(jsonrpc_line(payload))
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(
                f"could not send JSON-RPC message: {exc}\n"
                f"stderr tail:\n{self._stderr_text()}"
            ) from exc

    def receive(self, timeout: float = MCP_TIMEOUT_SECONDS) -> dict[str, Any]:
        try:
            line = self._stdout_lines.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError(
                f"MCP server did not respond within {timeout:g}s\n"
                f"stderr tail:\n{self._stderr_text()}"
            ) from exc
        if line is None:
            raise RuntimeError(
                "MCP server closed stdout\n"
                f"stderr tail:\n{self._stderr_text()}"
            )
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"invalid JSON-RPC response: {line!r}\n"
                f"stderr tail:\n{self._stderr_text()}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"JSON-RPC response is not an object: {payload!r}\n"
                f"stderr tail:\n{self._stderr_text()}"
            )
        if payload.get("error") is not None:
            raise RuntimeError(
                f"JSON-RPC error: {json.dumps(payload['error'])}\n"
                f"stderr tail:\n{self._stderr_text()}"
            )
        return payload

    def request(
        self,
        identifier: object,
        method: str,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": method,
        }
        if params is not None:
            payload["params"] = dict(params)
        self.send(payload)
        response = self.receive()
        if response.get("id") != identifier:
            raise RuntimeError(
                f"MCP response id mismatch: sent {identifier!r}, "
                f"received {response.get('id')!r}"
            )
        return response

    def notify(self, method: str) -> None:
        self.send({"jsonrpc": "2.0", "method": method})

    def _signal_process_group(
        self, process: subprocess.Popen[Any], number: signal.Signals
    ) -> None:
        os.killpg(process.pid, number)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stop_problem: str | None = None

        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass

        try:
            self._signal_process_group(self.process, signal.SIGTERM)
        except ProcessLookupError:
            pass

        if self.process.poll() is None:
            try:
                self.process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    self._signal_process_group(self.process, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    stop_problem = "MCP child did not exit after SIGKILL"

        self._stdout_thread.join(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        self._stderr_thread.join(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        if stop_problem is not None:
            raise RuntimeError(stop_problem)


def native_environment(root: Path, data_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["JARVIS_DATA_DIR"] = str(data_dir)
    paths = [str(root / "bin")]
    if env.get("NATIVE_SMOKE_EXTRA_PATH"):
        paths.append(env["NATIVE_SMOKE_EXTRA_PATH"])
    # Language indexers are deliberately not bundled; keep the directory of
    # the parent environment's scip-python visible after sanitizing PATH.
    indexer = shutil.which("scip-python")
    if indexer:
        directory = str(Path(indexer).parent)
        if directory not in paths:
            paths.append(directory)
    paths.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    env["PATH"] = os.pathsep.join(paths)
    # The smoke must not share the default Zoekt port with an unrelated
    # jarvis/zoekt-webserver already running on this machine.
    env["JARVIS_ZOEKT_PORT"] = str(free_port())
    return env


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_checked(
    command: str,
    arguments: list[str],
    env: dict[str, str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [command, *arguments],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout:g}s: {command} {' '.join(arguments)}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): "
            f"{command} {' '.join(arguments)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def stop_watch_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
    except ProcessLookupError:
        return
    process.kill()
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("watch child did not exit after SIGKILL") from exc


def stop_dashboard_process(
    process: subprocess.Popen[Any],
    signal_group=lambda process, number: os.killpg(process.pid, number),
) -> None:
    if process.poll() is not None:
        return
    try:
        signal_group(process, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    signal_group(process, signal.SIGKILL)
    process.kill()
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "dashboard child did not exit after SIGKILL"
        ) from exc


def probe_watch(jarvis: str, repo: Path, env: dict[str, str]) -> None:
    process = subprocess.Popen(
        [jarvis, "watch", str(repo), "--slug", "native-smoke", "--debounce", "5"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        time.sleep(WATCH_PROBE_SECONDS)
        if process.poll() is not None:
            raise RuntimeError(
                "jarvis watch exited before the smoke probe completed "
                f"(return code {process.returncode})"
            )
    finally:
        stop_watch_process(process)


def fetch_dashboard_asset(url: str) -> tuple[int, str, str]:
    with urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return (
            int(response.status),
            response.headers.get_content_type(),
            response.read().decode("utf-8"),
        )


def probe_dashboard(jarvis: str, env: dict[str, str]) -> None:
    port = free_port()
    process = subprocess.Popen(
        [jarvis, "dashboard", "--no-open", "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + DASHBOARD_TIMEOUT_SECONDS
        responses = []
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "jarvis dashboard exited before responding "
                    f"(return code {process.returncode})"
                )
            try:
                responses = [
                    fetch_dashboard_asset(f"http://127.0.0.1:{port}/"),
                    fetch_dashboard_asset(
                        f"http://127.0.0.1:{port}/style.css"
                    ),
                ]
                break
            except OSError:
                time.sleep(DASHBOARD_POLL_SECONDS)
        else:
            raise RuntimeError(
                "jarvis dashboard did not respond within "
                f"{DASHBOARD_TIMEOUT_SECONDS:g}s"
            )
        for path, response in zip(("/", "/style.css"), responses):
            assert_dashboard_asset(path, *response)
    finally:
        stop_dashboard_process(process)


def call_tool(
    client: McpStdioClient, identifier: int, name: str, arguments: dict[str, object]
) -> dict[str, Any]:
    return client.request(
        identifier,
        "tools/call",
        {"name": name, "arguments": arguments},
    )


def run_smoke(
    root: Path, repo: Path, data_dir: Path, expected_version: str
) -> None:
    root = root.resolve()
    repo = repo.resolve()
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    env = native_environment(root, data_dir)
    jarvis = str(root / "bin" / "jarvis")
    jarvis_server = str(root / "bin" / "jarvis-server")

    version = run_checked(jarvis, ["--version"], env)
    assert_exact_version(version.stdout, expected_version)
    help_output = run_checked(jarvis, ["--help"], env)
    assert_help_output(help_output.stdout)

    run_checked(jarvis, ["index", str(repo), "--slug", "native-smoke"], env)
    probe_watch(jarvis, repo, env)
    probe_dashboard(jarvis, env)

    client = McpStdioClient(jarvis_server, env)
    try:
        client.request(
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "jarvis-native-smoke", "version": "1.0"},
            },
        )
        client.notify("notifications/initialized")
        listed = client.request(2, "tools/list")
        tools = listed.get("result", {}).get("tools")
        if not isinstance(tools, list) or len(tools) != 10:
            actual = len(tools) if isinstance(tools, list) else tools
            raise RuntimeError(f"expected 10 MCP tools, found {actual!r}")

        references = call_tool(
            client,
            3,
            "findReferences",
            {"repo": "native-smoke", "symbol": "greet"},
        )
        if "greeter.py" not in text_body(references.get("result", {})):
            raise RuntimeError("findReferences did not return greeter.py")

        search = call_tool(
            client,
            4,
            "searchCode",
            {"query": "hello", "repo": "native-smoke"},
        )
        if "greeter.py" not in text_body(search.get("result", {})):
            raise RuntimeError("searchCode did not return greeter.py")

        semantic = call_tool(
            client,
            5,
            "semanticSearch",
            {"repo": "native-smoke", "query": "greeting"},
        )
        assert_semantic_unavailable(semantic)
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)

    try:
        run_smoke(
            args.root, args.repo, args.data_dir, args.expected_version
        )
    except Exception as exc:
        print(f"native smoke: FAIL: {exc}", file=sys.stderr)
        return 1
    print("native smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
