"""Apply current-profile PostgreSQL migrations in filename order.

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

_TRACK_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename        text PRIMARY KEY,
  checksum_sha256 text NOT NULL,
  byte_size       bigint NOT NULL,
  applied_at      timestamptz NOT NULL DEFAULT now()
)
"""


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
        raise MigrationError("migration advisory lock was not owned at cleanup")


def _prepare_tracking_table(connection: Any) -> None:
    with connection.cursor() as cur:
        cur.execute(_TRACK_TABLE)
    connection.commit()


def _load_applied(connection: Any) -> dict[str, tuple[str | None, int | None]]:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT filename, checksum_sha256, byte_size
                FROM schema_migrations
                ORDER BY filename
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        raise MigrationError(
            "local migration history is from an unsupported prior profile; "
            "reset/rebuild this profile"
        ) from exc
    applied: dict[str, tuple[str | None, int | None]] = {}
    for filename, checksum, byte_size in rows:
        if filename in applied:
            raise MigrationError(f"duplicate migration history row: {filename}")
        applied[str(filename)] = (
            str(checksum) if checksum is not None else None,
            int(byte_size) if byte_size is not None else None,
        )
    return applied


def _verify_current_history(
    connection: Any,
    migrations: Sequence[MigrationFile],
) -> set[str]:
    available = {item.filename: item for item in migrations}
    applied = _load_applied(connection)
    unsupported = sorted(set(applied) - set(available))
    if unsupported:
        raise MigrationError(
            "local migration history contains unsupported entries "
            f"({', '.join(unsupported)}); reset/rebuild this profile"
        )

    for filename, (stored_checksum, stored_size) in applied.items():
        migration = available[filename]
        if stored_checksum is None or stored_size is None:
            raise MigrationError(
                "local migration history is from an unsupported prior profile; "
                "reset/rebuild this profile"
            )
        if (
            not isinstance(stored_checksum, str)
            or _SHA256.fullmatch(stored_checksum) is None
            or stored_checksum != migration.checksum_sha256
            or stored_size != migration.byte_size
        ):
            raise MigrationError(
                f"applied migration is not current: {filename}; reset/rebuild this profile"
            )
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
        applied = _verify_current_history(connection, migrations)
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
