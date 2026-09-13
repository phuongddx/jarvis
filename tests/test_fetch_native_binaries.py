"""Offline tests for jarvis's pinned native-binary fetcher."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
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
