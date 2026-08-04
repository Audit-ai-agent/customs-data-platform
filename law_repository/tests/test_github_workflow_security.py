from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
IMMUTABLE_ACTION = re.compile(r"^[^#]+@[0-9a-f]{40}(?:\s+#.*)?$")


def test_every_external_action_is_pinned_to_an_immutable_commit() -> None:
    floating: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped.startswith("uses:") and not stripped.startswith("- uses:"):
                continue
            reference = stripped.split("uses:", 1)[1].strip()
            if reference.startswith("./"):
                continue
            if not IMMUTABLE_ACTION.fullmatch(reference):
                floating.append(f"{workflow.name}:{line_number}: {reference}")

    assert floating == []


def test_ci_is_the_only_fast_read_only_automation_path() -> None:
    workflows = sorted(path.name for path in WORKFLOWS.glob("*.yml"))
    assert workflows == ["ci.yml"]

    workflow = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "contents: read" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "paths:" in workflow

    forbidden = (
        "secrets.",
        "environment:",
        "workflow_dispatch:",
        "schedule:",
        "upload-artifact",
        "docker build",
        "docker push",
        "git push",
        "git tag ",
    )
    assert all(value not in workflow for value in forbidden)
