"""Assert the local release version in pyproject.toml is readable.

pyproject.toml is the only local release-version source.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_declared_versions(root: Path) -> dict[str, str]:
    """Map the sole local release-version declaration to its value."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    return {
        "pyproject.toml [project].version": pyproject["project"]["version"]
    }


def check(root: Path) -> list[str]:
    """Return problems; an empty list means the version is readable."""
    try:
        declared = read_declared_versions(root)
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return [f"could not read release version: {exc}"]
    if len(declared) != 1:
        return ["expected exactly one local release version declaration"]
    return []


def main() -> int:
    problems = check(REPO_ROOT)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("versions consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
