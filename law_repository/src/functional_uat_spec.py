"""Dependency-free facts shared by local Data profiles.

Keeping this small specification separate lets the default synthetic fixture
and its unit tests assert the same three-law boundary without importing
database clients, ingestion code, or model packages.
"""

from __future__ import annotations

from datetime import date

FUNCTIONAL_UAT_AS_OF = date(2026, 8, 5)
FUNCTIONAL_UAT_LAW_IDS: tuple[str, str, str] = ("001556", "002421", "006392")
FUNCTIONAL_UAT_LAWS: tuple[tuple[str, str], tuple[str, str], tuple[str, str]] = (
    (FUNCTIONAL_UAT_LAW_IDS[0], "관세법"),
    (FUNCTIONAL_UAT_LAW_IDS[1], "관세법 시행령"),
    (FUNCTIONAL_UAT_LAW_IDS[2], "관세법 시행규칙"),
)
FUNCTIONAL_UAT_TARGET_LAWS: tuple[dict[str, str], ...] = tuple(
    {"law_id": law_id, "name": law_name} for law_id, law_name in FUNCTIONAL_UAT_LAWS
)

__all__ = [
    "FUNCTIONAL_UAT_AS_OF",
    "FUNCTIONAL_UAT_LAW_IDS",
    "FUNCTIONAL_UAT_LAWS",
    "FUNCTIONAL_UAT_TARGET_LAWS",
]
