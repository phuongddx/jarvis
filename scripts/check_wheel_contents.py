"""Assert release wheels ship compiled jarvis modules, the dashboard
assets, and no readable source.

The compiled-wheel pipeline can silently regress to publishing readable
source (JARVIS_COMPILE unset in CI, the build_py override dropped, a Cython
.c intermediate packaged by accident), and a package-data regression can
silently drop the dashboard assets. Run this over every built wheel before
publication; an empty report is the only green path.
"""

import sys
import zipfile

ALLOWED_SOURCE = {"jarvis/__init__.py", "jarvis/scip_pb2.py"}
EXPECTED_ASSETS = {
    "jarvis/dashboard_assets/index.html",
    "jarvis/dashboard_assets/app.js",
    "jarvis/dashboard_assets/style.css",
}
SOURCE_SUFFIXES = (".py", ".pyx", ".pxd", ".c")


def check(wheel_path: str) -> list[str]:
    names = zipfile.ZipFile(wheel_path).namelist()
    problems = []
    compiled = [n for n in names if n.startswith("jarvis/") and n.endswith(".so")]
    leaked = sorted(
        n
        for n in names
        if n.startswith("jarvis/")
        and n.endswith(SOURCE_SUFFIXES)
        and n not in ALLOWED_SOURCE
    )
    missing = sorted(ALLOWED_SOURCE - set(names))
    missing_assets = sorted(EXPECTED_ASSETS - set(names))
    if not compiled:
        problems.append(f"{wheel_path}: no compiled jarvis/*.so modules")
    if leaked:
        problems.append(f"{wheel_path}: readable source leaked: {leaked}")
    if missing:
        problems.append(f"{wheel_path}: missing expected files: {missing}")
    if missing_assets:
        problems.append(f"{wheel_path}: missing dashboard assets: {missing_assets}")
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_wheel_contents.py <wheel> [<wheel>...]", file=sys.stderr)
        return 2
    problems = [problem for wheel in argv for problem in check(wheel)]
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{len(argv)} wheel(s) verified: compiled modules only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
