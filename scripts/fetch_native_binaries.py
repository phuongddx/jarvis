"""Fetch pinned public SCIP and Zoekt binaries for native packaging."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_REPO = "https://github.com/jarvis-intelligence/jarvis-index/releases/download"
PLATFORM_RE = re.compile(r"^(darwin|linux)_(arm64|amd64)$")


def split_platform(platform: str) -> tuple[str, str]:
    match = PLATFORM_RE.fullmatch(platform)
    if match is None:
        raise ValueError(f"unsupported native platform {platform!r}")
    return match.group(1), match.group(2)


def _pin(tool: str) -> str:
    return (ROOT / f"{tool.upper()}_COMMIT").read_text().strip()


def asset_urls(platform: str) -> list[tuple[str, str, tuple[str, ...]]]:
    os_name, arch = split_platform(platform)
    scip_tag = f"scip-{_pin('scip')}"
    zoekt_tag = f"zoekt-{_pin('zoekt')}"
    return [
        (
            f"{RELEASE_REPO}/{scip_tag}/scip-{os_name}-{arch}.tar.gz",
            f"{RELEASE_REPO}/{scip_tag}/scip-{os_name}-{arch}.tar.gz.sha256",
            ("scip",),
        ),
        (
            f"{RELEASE_REPO}/{zoekt_tag}/zoekt-{os_name}-{arch}.tar.gz",
            f"{RELEASE_REPO}/{zoekt_tag}/zoekt-{os_name}-{arch}.tar.gz.sha256",
            ("zoekt-git-index", "zoekt-webserver"),
        ),
    ]


def download_to(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response:
        destination.write_bytes(response.read())


def fetch(platform: str, output: Path, only: str | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    if only is not None and only not in {"scip", "zoekt"}:
        raise ValueError("--only must be 'scip' or 'zoekt'")
    for archive_url, checksum_url, members in asset_urls(platform):
        tool = "scip" if members == ("scip",) else "zoekt"
        if only is not None and tool != only:
            continue
        with tempfile.TemporaryDirectory(prefix="jarvis-native-") as temporary:
            work = Path(temporary)
            archive = work / "archive.tar.gz"
            checksum = work / "archive.sha256"
            download_to(archive_url, archive)
            download_to(checksum_url, checksum)
            expected = checksum.read_text().split()[0]
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if expected != actual:
                raise RuntimeError(
                    f"checksum mismatch for {archive_url}: "
                    f"expected {expected}, got {actual}"
                )
            with tarfile.open(archive, "r:gz") as tar:
                selected = []
                for name in members:
                    member = tar.getmember(name)
                    if not member.isfile():
                        raise RuntimeError(f"release member is not a file: {name}")
                    selected.append(member)
                tar.extractall(work, members=selected, filter="data")
            for name in members:
                destination = output / name
                shutil.move(work / name, destination)
                destination.chmod(0o755)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--only")
    args = parser.parse_args(argv)
    try:
        fetch(args.platform, args.output, args.only)
    except (OSError, RuntimeError, tarfile.TarError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
