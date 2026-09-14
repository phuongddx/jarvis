"""Tests for native release build and publication orchestration."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "ci_native_build.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-native.yml"
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def test_native_builder_excludes_dash_dependent_setup_tests():
    content = BUILD_SCRIPT.read_text()
    assert '--ignore=tests/test_setup_sh.py' in content
    assert 'uv run pytest -m "not integration"' in content
    assert "uv pip uninstall --python .native-release/package/bin/python" in content
    assert "setuptools wheel Cython" in content


def test_native_builder_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(BUILD_SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def workflow_job(name: str) -> str:
    content = WORKFLOW.read_text()
    headings = list(re.finditer(r"(?m)^  ([A-Za-z-]+):$", content))
    positions = [index for index, match in enumerate(headings) if match.group(1) == name]
    assert len(positions) == 1
    heading_index = positions[0]
    start = headings[heading_index].end()
    end = (
        headings[heading_index + 1].start()
        if heading_index + 1 < len(headings)
        else len(content)
    )
    return content[start:end]


def test_workflow_supports_build_only_manual_dispatch():
    content = WORKFLOW.read_text()
    assert "workflow_dispatch:" in content
    for job in ("stage-release", "validate-homebrew", "publish-formula"):
        assert "github.event_name == 'release'" in workflow_job(job)


def test_homebrew_validation_smokes_the_installed_formula():
    job = workflow_job("validate-homebrew")
    assert "actions/checkout@v4" in job
    assert "actions/setup-node@v4" in job
    assert "prefix=\"$(brew --prefix jarvis)\"" in job
    assert "--root \"$prefix\"" in job
    assert "--expected-version \"$version\"" in job
    assert "native-bin/universal-ctags" in job
    assert "rm \"$prefix/libexec/_internal/native-bin/scip\"" in job
    assert "brew reinstall --formula ./jarvis.rb" in job
    assert job.count("scripts/native_smoke.py") == 2


def test_unit_workflow_installs_native_packaging_dependencies():
    content = TEST_WORKFLOW.read_text()
    assert "uv sync --extra semantic --group native" in content
