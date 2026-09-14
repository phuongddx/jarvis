"""Offline tests for jarvis's pinned native-binary fetcher."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import socket
import tarfile
import urllib.error
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_native_binaries.py"
spec = importlib.util.spec_from_file_location("fetch_native_binaries", SCRIPT)
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)


def test_splits_supported_platforms():
    assert fetcher.split_platform("darwin_arm64") == ("darwin", "arm64")
    assert fetcher.split_platform("linux_amd64") == ("linux", "amd64")


def test_rejects_unsupported_platform():
    try:
        fetcher.split_platform("windows_amd64")
    except ValueError as exc:
        assert "windows_amd64" in str(exc)
    else:
        raise AssertionError("unsupported platform must fail")


def test_download_retries_transient_504_then_succeeds(tmp_path, monkeypatch):
    destination = tmp_path / "native.tar.gz"
    attempts: list[str] = []
    sleeps: list[float] = []
    response = io.BytesIO(b"native archive")

    class StreamingResponse:
        def read(self) -> bytes:
            return response.getvalue()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            response.close()

    def fake_urlopen(url: str, timeout: int):
        attempts.append(url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(url, 504, "Gateway Time-out", None, None)
        return StreamingResponse()

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    fetcher.download_to("archive-url", destination)

    assert attempts == ["archive-url", "archive-url"]
    assert sleeps == [2]
    assert destination.read_bytes() == b"native archive"


def test_download_fails_after_three_transient_504s(tmp_path, monkeypatch):
    destination = tmp_path / "native.tar.gz"
    attempts: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(url: str, timeout: int):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 504, "Gateway Time-out", None, None)

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    try:
        fetcher.download_to("archive-url", destination)
    except urllib.error.HTTPError as exc:
        assert exc.code == 504
    else:
        raise AssertionError("persistent transient failures must fail")

    assert attempts == ["archive-url"] * 3
    assert sleeps == [2, 4]
    assert not destination.exists()


def test_download_does_not_retry_non_transient_http_error(tmp_path, monkeypatch):
    destination = tmp_path / "native.tar.gz"
    attempts: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(url: str, timeout: int):
        attempts.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    try:
        fetcher.download_to("archive-url", destination)
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("non-transient failures must fail immediately")

    assert attempts == ["archive-url"]
    assert sleeps == []


def test_download_retries_socket_timeout(tmp_path, monkeypatch):
    destination = tmp_path / "native.tar.gz"
    attempts: list[str] = []
    sleeps: list[float] = []

    class StreamingResponse:
        def read(self) -> bytes:
            return b"native archive"

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(url: str, timeout: int):
        attempts.append(url)
        if len(attempts) == 1:
            raise socket.timeout("download timed out")
        return StreamingResponse()

    monkeypatch.setattr(fetcher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    fetcher.download_to("archive-url", destination)

    assert attempts == ["archive-url", "archive-url"]
    assert sleeps == [2]
    assert destination.read_bytes() == b"native archive"


def test_asset_urls_follow_public_pins():
    urls = fetcher.asset_urls("darwin_arm64")
    assert len(urls) == 2
    assert urls[0][0].startswith(
        "https://github.com/jarvis-intelligence/jarvis-index/releases/download/scip-"
    )
    assert urls[0][2] == ("scip",)
    assert urls[1][2] == ("zoekt-git-index", "zoekt-webserver")


def test_fetch_verifies_checksum_and_extracts_scip(tmp_path, monkeypatch):
    output = tmp_path / "native"
    output.mkdir()
    archives: dict[str, bytes] = {}

    def fake_download(url: str, destination: Path) -> None:
        if url in archives:
            destination.write_bytes(archives[url])
            return
        raw = io.BytesIO()
        data = b"#!/bin/sh\ntrue\n"
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            info = tarfile.TarInfo("scip")
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
        blob = raw.getvalue()
        archives[url] = blob
        archives[url + ".sha256"] = (
            hashlib.sha256(blob).hexdigest() + "  archive.tar.gz\n"
        ).encode()
        destination.write_bytes(blob)

    monkeypatch.setattr(fetcher, "download_to", fake_download)
    fetcher.fetch("darwin_arm64", output, only="scip")
    assert (output / "scip").read_bytes().startswith(b"#!/bin/sh")
