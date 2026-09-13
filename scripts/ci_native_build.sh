#!/usr/bin/env bash
# Build one standalone jarvis archive. Linux builds in manylinux containers.
set -euo pipefail

INSIDE=0
if [ "${1:-}" = "--inside" ]; then
    INSIDE=1
    shift
fi
PLATFORM="${1:?usage: ci_native_build.sh [--inside] PLATFORM}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

inside_build() {
    local platform="$1"
    local python="3.12"
    if [[ "$platform" == linux_* ]]; then
        python="/opt/python/cp312-cp312/bin/python3"
    fi

    if ! command -v uv >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    if ! command -v file >/dev/null 2>&1; then
        yum install -y file
    fi

    export UV_PYTHON="$python"
    export UV_PROJECT_ENVIRONMENT="$PWD/.native-release/project"
    rm -rf .native-release
    mkdir -p .native-release/wheels .native-release/native \
             .native-release/app .native-release/release

    uv sync --group native
    uv run pytest -m "not integration" -rs
    JARVIS_COMPILE=1 uv build --python "$UV_PYTHON" --wheel \
        --out-dir .native-release/wheels

    uv venv --python "$UV_PYTHON" .native-release/package
    uv pip install --python .native-release/package/bin/python \
        .native-release/wheels/jarvis_mcp-*.whl 'watchdog>=4' pyinstaller==6.14.1

    uv run python scripts/fetch_native_binaries.py \
        --platform "$platform" --output .native-release/native
    JARVIS_NATIVE_BIN_DIR="$PWD/.native-release/native" \
    JARVIS_APP_OUTPUT_DIR="$PWD/.native-release/app" \
    JARVIS_PYINSTALLER_WORK_DIR="$PWD/.native-release/pyinstaller-work" \
        .native-release/package/bin/python scripts/run_pyinstaller.py

    local version
    version="$(uv version --short)"
    uv run python scripts/stage_native_release.py \
        --app-dir .native-release/app --native-bin-dir .native-release/native \
        --output-dir .native-release/release --version "$version" \
        --platform "$platform"
    uv run python scripts/check_native_package.py \
        ".native-release/release/jarvis_${version}_${platform}.tar.gz" \
        --platform "$platform"
}

if [ "$INSIDE" -eq 1 ]; then
    inside_build "$PLATFORM"
    exit 0
fi

case "$PLATFORM" in
    darwin_arm64 | darwin_amd64)
        inside_build "$PLATFORM"
        ;;
    linux_amd64)
        docker run --rm -v "$PWD:$PWD" -w "$PWD" \
            quay.io/pypa/manylinux_2_28_x86_64 \
            bash scripts/ci_native_build.sh --inside "$PLATFORM"
        ;;
    linux_arm64)
        docker run --rm -v "$PWD:$PWD" -w "$PWD" \
            quay.io/pypa/manylinux_2_28_aarch64 \
            bash scripts/ci_native_build.sh --inside "$PLATFORM"
        ;;
    *)
        echo "error: unsupported platform: $PLATFORM" >&2
        exit 2
        ;;
esac
