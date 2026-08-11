"""Import and explicitly activate one locally staged official HSK/CCCT archive.

This tool has no downloader, service key, scheduler, or clone fallback.  An
operator first places a complete, hash-bound archive under the ignored local
``_local/data/official-archives/hsk-ccct/YYYY-MM-DD`` directory.  ``import``
persists its normalized PostgreSQL history without changing the current Neo4j
projection; ``--activate`` is the separate service-visible decision.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.hsk_ccct_history import (  # noqa: E402
    HskCcctArchiveError,
    activate_archive,
    archive_directory,
    capture_active_archive_catalog,
    import_archive,
    load_archive,
)


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    if parsed < date(2026, 1, 1):
        raise argparse.ArgumentTypeError("archive date must be on or after 2026-01-01")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "import", "activate"):
        command = commands.add_parser(name)
        command.add_argument("--as-of", required=True, type=_date)
    commands.choices["import"].add_argument(
        "--activate",
        action="store_true",
        help="also atomically add this imported date to the active pointer",
    )
    commands.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            catalog = capture_active_archive_catalog(verify_assets=True)
            dates = ",".join(archive.archive_as_of.isoformat() for archive in catalog.archives)
            print(
                f"active official HSK/CCCT archives: {len(catalog.archives)}"
                + (f" ({dates})" if dates else "")
            )
            return 0
        directory = archive_directory(args.as_of)
        if args.command == "validate":
            archive = load_archive(directory)
            print(
                "official HSK/CCCT archive verified: "
                f"{archive.manifest.archive_as_of.isoformat()} "
                f"(HSK {len(archive.hsk_nodes)}, CCCT queries {len(archive.ccct_dataset.queries)})"
            )
            return 0
        if args.command == "import":
            imported = import_archive(directory, activate=bool(args.activate))
            activation = " and activated" if args.activate else ""
            print(
                f"official HSK/CCCT archive imported{activation}: {imported.archive_as_of.isoformat()}"
            )
            return 0
        if args.command == "activate":
            catalog = activate_archive(directory)
            print(
                f"official HSK/CCCT archive activated: {args.as_of.isoformat()} ({len(catalog.archives)} total)"
            )
            return 0
    except HskCcctArchiveError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
