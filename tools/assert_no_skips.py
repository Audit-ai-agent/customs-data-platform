"""Make a Live Gate fail when pytest reports any skipped test."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def totals(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    # A testsuites root and nested suites can both carry totals. Prefer direct
    # children to avoid double counting nested aggregate nodes.
    if root.tag == "testsuites":
        suites = [node for node in root if node.tag == "testsuite"]
    return {
        name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: assert_no_skips.py <pytest-junit.xml>")
        return 2
    path = Path(argv[1])
    summary = totals(path)
    print("Live JUnit summary: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    if summary["tests"] == 0 or any(summary[key] for key in ("failures", "errors", "skipped")):
        print("Live Gate failed: zero tests, failures/errors, or skips are not allowed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
