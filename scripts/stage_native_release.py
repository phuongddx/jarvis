"""Shape and archive a PyInstaller distribution for native release."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

NATIVE_BINARIES = ("scip", "zoekt-git-index", "zoekt-webserver")
PLATFORM_RE = re.compile(r"^(darwin|linux)_(arm64|amd64)$")


def is_executable(path: Path) -> bool:
    return path.is_file() and path.stat().st_mode & 0o111 != 0


def _validate_platform(platform: str) -> None:
    if PLATFORM_RE.fullmatch(platform) is None:
        raise ValueError(f"unsupported native platform {platform!r}")


def _validate_inputs(app_dir: Path, native_bin_dir: Path) -> None:
    launcher = app_dir / "jarvis" / "jarvis"
    if not is_executable(launcher):
        raise RuntimeError(f"PyInstaller launcher is not executable: {launcher}")
    for name in NATIVE_BINARIES:
        native = native_bin_dir / name
        if not is_executable(native):
            raise RuntimeError(f"native binary is not executable: {native}")
    if not (app_dir / "jarvis" / "_internal").is_dir():
        raise RuntimeError(f"PyInstaller runtime is missing: {app_dir}/jarvis/_internal")


def _build_tree(app_dir: Path, native_bin_dir: Path, root: Path) -> None:
    libexec = root / "libexec"
    libexec.mkdir(parents=True)
    shutil.copytree(
        app_dir / "jarvis" / "_internal",
        libexec / "_internal",
        symlinks=True,
    )
    shutil.copy2(app_dir / "jarvis" / "jarvis", libexec / "jarvis")
    native_dest = libexec / "_internal" / "native-bin"
    native_dest.mkdir(parents=True, exist_ok=True)
    for name in NATIVE_BINARIES:
        shutil.copy2(native_bin_dir / name, native_dest / name)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name in ("jarvis", "jarvis-server"):
        os.symlink("../libexec/jarvis", bin_dir / name)


def _write_archive(root: Path, archive: Path, checksum: Path) -> None:
    with tarfile.open(archive, "w:gz", dereference=False) as tar:
        tar.add(root, arcname="jarvis")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n")


def stage_native_release(
    app_dir: Path,
    native_bin_dir: Path,
    output_dir: Path,
    version: str,
    platform: str,
) -> tuple[Path, Path]:
    _validate_platform(platform)
    _validate_inputs(app_dir, native_bin_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir / "jarvis"
    if root.exists() or root.is_symlink():
        shutil.rmtree(root)
    _build_tree(app_dir, native_bin_dir, root)
    archive = output_dir / f"jarvis_{version}_{platform}.tar.gz"
    checksum = archive.with_name(archive.name + ".sha256")
    _write_archive(root, archive, checksum)
    return archive, checksum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--native-bin-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args(argv)
    try:
        stage_native_release(
            args.app_dir,
            args.native_bin_dir,
            args.output_dir,
            args.version,
            args.platform,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
