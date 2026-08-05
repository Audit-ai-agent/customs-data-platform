"""Apply immutable, checksum-attested PostgreSQL migrations in filename order.

The runner owns one PostgreSQL advisory lock for the complete migration
sequence.  Previously applied files are byte-hashed on every run; editing,
removing, or replacing an applied migration fails before new SQL is executed.

Run from ``law_repository`` with ``python scripts/migrate.py``.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_LOCK_KEY = 5571026467717076307
_MIGRATION_NAME = re.compile(r"^(?P<number>[0-9]{3})_[a-z0-9_]+\.sql$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# These hashes adopt the pre-checksum 001..003 rows without silently trusting
# whatever bytes happen to be present on the first hardened run.  They are
# computed from the Git text form (LF); a Windows checkout may expose the same
# tracked text as CRLF, so legacy adoption compares that normalized form and
# then records the exact local bytes it executed.
LEGACY_BASELINE_CHECKSUMS = {
    "001_init.sql": "a082242873b48234b149dcb2407e436e797a658f9e3ad843109250360f846ca1",
    "002_parent_article_pk.sql": (
        "c70daf0ddf2e7bc637f16b1f4ee19bd5c4f95702e8678fc58a86219377e93947"
    ),
    "003_ccct_requirements.sql": (
        "af34b01bb04694da56e8846103b3f1d541b7a11b987cfa8900eeb3c61d2170cc"
    ),
}

# Existing local Docker volumes may already record this removed migration.
# Leaving that state untouched is safe because no supported profile reads the
# retired tables; new profile stores intentionally never create them.
RETIRED_MIGRATIONS = frozenset({"004_knowledge_generations.sql"})

_TRACK_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename        text PRIMARY KEY,
  checksum_sha256 text,
  byte_size       bigint,
  applied_at      timestamptz NOT NULL DEFAULT now()
)
"""
_TRACK_COLUMNS = (
    "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum_sha256 text",
    "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS byte_size bigint",
)
_TRACK_HARDEN = (
    "ALTER TABLE schema_migrations ALTER COLUMN checksum_sha256 SET NOT NULL",
    "ALTER TABLE schema_migrations ALTER COLUMN byte_size SET NOT NULL",
)


class MigrationError(RuntimeError):
    """Migration history is unsafe or cannot be applied."""


@dataclass(frozen=True, slots=True)
class MigrationFile:
    number: int
    filename: str
    path: Path
    checksum_sha256: str
    byte_size: int
    sql: str


def _legacy_baseline_matches(migration: MigrationFile, expected_checksum: str) -> bool:
    """Match a known pre-checksum migration across Git line-ending checkout modes."""

    normalized = migration.sql.replace("\r\n", "\n").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest() == expected_checksum


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[MigrationFile, ...]:
    migrations: list[MigrationFile] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        raw = path.read_bytes()
        try:
            sql = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration is not strict UTF-8: {path.name}") from exc
        if not sql.strip():
            raise MigrationError(f"migration is empty: {path.name}")
        migrations.append(
            MigrationFile(
                number=int(match.group("number")),
                filename=path.name,
                path=path,
                checksum_sha256=hashlib.sha256(raw).hexdigest(),
                byte_size=len(raw),
                sql=sql,
            )
        )
    if not migrations:
        return ()
    numbers = [item.number for item in migrations]
    expected = list(range(1, len(migrations) + 1))
    if numbers != expected:
        raise MigrationError(f"migration numbers must be contiguous from 001: {numbers!r}")
    return tuple(migrations)


def _acquire_lock(connection: Any) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        row = cur.fetchone()
    connection.commit()
    if row is None or row[0] is not True:
        raise MigrationError("another migration writer owns the advisory lock")


def _unlock_migration_writer(connection: Any) -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        row = cur.fetchone()
    connection.commit()
    if row is None or row[0] is not True:
        raise MigrationError("migration advisory lock was not owned at release")


def _prepare_tracking_table(connection: Any) -> None:
    with connection.cursor() as cur:
        cur.execute(_TRACK_TABLE)
        for statement in _TRACK_COLUMNS:
            cur.execute(statement)
    connection.commit()


def _load_applied(connection: Any) -> dict[str, tuple[str | None, int | None]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT filename, checksum_sha256, byte_size
            FROM schema_migrations
            ORDER BY filename
            """
        )
        rows = cur.fetchall()
    applied: dict[str, tuple[str | None, int | None]] = {}
    for filename, checksum, byte_size in rows:
        if filename in applied:
            raise MigrationError(f"duplicate migration history row: {filename}")
        applied[str(filename)] = (
            str(checksum) if checksum is not None else None,
            int(byte_size) if byte_size is not None else None,
        )
    return applied


def _verify_and_adopt_history(
    connection: Any,
    migrations: Sequence[MigrationFile],
) -> set[str]:
    available = {item.filename: item for item in migrations}
    applied = _load_applied(connection)
    missing_files = sorted(set(applied) - set(available) - RETIRED_MIGRATIONS)
    if missing_files:
        raise MigrationError("applied migration files are missing: " + ", ".join(missing_files))

    with connection.cursor() as cur:
        for filename, (stored_checksum, stored_size) in applied.items():
            if filename in RETIRED_MIGRATIONS and filename not in available:
                continue
            migration = available[filename]
            if stored_checksum is None or stored_size is None:
                expected_legacy = LEGACY_BASELINE_CHECKSUMS.get(filename)
                if expected_legacy is None or not _legacy_baseline_matches(
                    migration, expected_legacy
                ):
                    raise MigrationError(f"cannot safely adopt checksum-less migration: {filename}")
                cur.execute(
                    """
                    UPDATE schema_migrations
                    SET checksum_sha256=%s, byte_size=%s
                    WHERE filename=%s
                      AND (checksum_sha256 IS NULL OR byte_size IS NULL)
                    """,
                    (
                        migration.checksum_sha256,
                        migration.byte_size,
                        filename,
                    ),
                )
                stored_checksum = migration.checksum_sha256
                stored_size = migration.byte_size
            if (
                not isinstance(stored_checksum, str)
                or _SHA256.fullmatch(stored_checksum) is None
                or stored_checksum != migration.checksum_sha256
                or stored_size != migration.byte_size
            ):
                raise MigrationError(f"applied migration was modified: {filename}")
        for statement in _TRACK_HARDEN:
            cur.execute(statement)
    connection.commit()
    return set(applied)


def apply_migrations(
    connection: Any,
    migrations: Sequence[MigrationFile],
    *,
    output: Callable[[str], None] = print,
) -> None:
    """Apply migrations on an existing connection under one writer lock."""

    locked = False
    try:
        _acquire_lock(connection)
        locked = True
        _prepare_tracking_table(connection)
        applied = _verify_and_adopt_history(connection, migrations)
        for migration in migrations:
            if migration.filename in applied:
                output(f"skip  {migration.filename} (checksum verified)")
                continue
            output(f"apply {migration.filename}")
            try:
                with connection.cursor() as cur:
                    cur.execute(migration.sql)
                    cur.execute(
                        """
                        INSERT INTO schema_migrations (
                          filename, checksum_sha256, byte_size
                        ) VALUES (%s, %s, %s)
                        """,
                        (
                            migration.filename,
                            migration.checksum_sha256,
                            migration.byte_size,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    except Exception:
        connection.rollback()
        raise
    finally:
        if locked:
            _unlock_migration_writer(connection)


def main() -> None:
    import psycopg

    from src.config import settings

    migrations = discover_migrations()
    if not migrations:
        print("적용할 마이그레이션 없음.")
        return
    with psycopg.connect(settings.postgres_dsn) as connection:
        apply_migrations(connection, migrations)
    print("완료.")


if __name__ == "__main__":
    main()
