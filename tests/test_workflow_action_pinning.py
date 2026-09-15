"""Guard against mutable major-version refs in GitHub Actions."""

from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"
USES_ACTION = re.compile(
    r"(?m)^\s*(?:- )?uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)\s*(?:#.*)?$"
)


def test_every_github_action_is_sha_pinned() -> None:
    mutable: list[str] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        for match in USES_ACTION.finditer(workflow.read_text()):
            if re.fullmatch(r"[0-9a-f]{40}", match.group("ref")) is None:
                mutable.append(f"{workflow.name}: {match.group(0).strip()}")

    assert mutable == []
