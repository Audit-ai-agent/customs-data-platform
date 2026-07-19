from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.migrate import (
    LEGACY_BASELINE_CHECKSUMS,
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    discover_migrations,
)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None
        self._all: list[tuple[Any, ...]] = []
        self.rowcount = -1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.split())
        self.connection.statements.append(normalized)
        self.rowcount = -1
        if normalized.startswith("SELECT pg_try_advisory_lock"):
            self._one = (self.connection.lock_available,)
            if self.connection.lock_available:
                self.connection.locked = True
            return
        if normalized.startswith("SELECT pg_advisory_unlock"):
            self._one = (self.connection.locked,)
            self.connection.locked = False
            return
        if normalized.startswith("SELECT filename, checksum_sha256, byte_size"):
            self._all = [
                (filename, checksum, size)
                for filename, (checksum, size) in sorted(self.connection.applied.items())
            ]
            return
        if normalized.startswith("UPDATE schema_migrations SET checksum_sha256"):
            assert params is not None
            checksum, size, filename = params
            self.connection.applied[str(filename)] = (str(checksum), int(size))
            self.rowcount = 1
            return
        if normalized.startswith("INSERT INTO schema_migrations"):
            assert params is not None
            filename, checksum, size = params
            self.connection.applied[str(filename)] = (str(checksum), int(size))
            self.rowcount = 1
            return
        if normalized.startswith(("CREATE TABLE IF NOT EXISTS schema_migrations", "ALTER TABLE")):
            return
        if self.connection.fail_sql_fragment and self.connection.fail_sql_fragment in query:
            raise RuntimeError("injected migration SQL failure")
        self.connection.applied_sql.append(query)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._all


class FakeConnection:
    def __init__(self) -> None:
        self.applied: dict[str, tuple[str | None, int | None]] = {}
        self.statements: list[str] = []
        self.applied_sql: list[str] = []
        self.lock_available = True
        self.locked = False
        self.commits = 0
        self.rollbacks = 0
        self.fail_sql_fragment: str | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_repository_migrations_are_contiguous_and_checksum_attested() -> None:
    migrations = discover_migrations()

    assert [item.number for item in migrations] == [1, 2, 3, 4]
    assert [item.filename for item in migrations[:3]] == list(LEGACY_BASELINE_CHECKSUMS)
    assert {
        item.filename: item.checksum_sha256 for item in migrations[:3]
    } == LEGACY_BASELINE_CHECKSUMS
    assert all(item.byte_size > 0 for item in migrations)


def test_discovery_rejects_gap_and_non_utf8(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "003_gap.sql").write_text("SELECT 3;", encoding="utf-8")
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(tmp_path)

    (tmp_path / "003_gap.sql").unlink()
    (tmp_path / "002_binary.sql").write_bytes(b"\xff")
    with pytest.raises(MigrationError, match="strict UTF-8"):
        discover_migrations(tmp_path)


def test_apply_adopts_only_known_legacy_hashes_and_is_idempotent() -> None:
    migrations = discover_migrations()
    connection = FakeConnection()
    connection.applied[migrations[0].filename] = (None, None)
    messages: list[str] = []

    apply_migrations(connection, migrations, output=messages.append)
    first_sql_count = len(connection.applied_sql)
    apply_migrations(connection, migrations, output=messages.append)

    assert connection.applied[migrations[0].filename] == (
        migrations[0].checksum_sha256,
        migrations[0].byte_size,
    )
    assert set(connection.applied) == {item.filename for item in migrations}
    assert len(connection.applied_sql) == first_sql_count
    assert connection.locked is False
    assert any("checksum verified" in message for message in messages)


def test_applied_migration_tamper_fails_before_new_sql() -> None:
    migrations = discover_migrations()
    connection = FakeConnection()
    connection.applied[migrations[0].filename] = ("0" * 64, migrations[0].byte_size)

    with pytest.raises(MigrationError, match="was modified"):
        apply_migrations(connection, migrations, output=lambda _message: None)

    assert connection.applied_sql == []
    assert connection.locked is False


def test_removed_applied_migration_fails_closed(tmp_path: Path) -> None:
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name != "001_init.sql":
            shutil.copy2(source, tmp_path / source.name)
    connection = FakeConnection()
    connection.applied["001_init.sql"] = (
        LEGACY_BASELINE_CHECKSUMS["001_init.sql"],
        3133,
    )

    # Discovery itself rejects the numbering gap before database access.
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(tmp_path)


def test_advisory_lock_contention_executes_no_migration() -> None:
    connection = FakeConnection()
    connection.lock_available = False

    with pytest.raises(MigrationError, match="another migration writer"):
        apply_migrations(connection, discover_migrations(), output=lambda _message: None)

    assert connection.applied_sql == []
    assert connection.locked is False


def test_generation_migration_is_additive_and_enforces_state_machine() -> None:
    migration = next(
        item for item in discover_migrations() if item.filename == "004_knowledge_generations.sql"
    )

    assert "ALTER TABLE law_articles" not in migration.sql
    assert "ALTER TABLE laws" not in migration.sql
    assert "CREATE TABLE knowledge_generations" in migration.sql
    assert "knowledge_generation_active_pointer" in migration.sql
    assert "knowledge_generation_store_attestations" in migration.sql
    assert "requires completed migrations 001..003" in migration.sql
    assert "knowledge_generation_datasets" in migration.sql
    assert "knowledge_generation_dataset_members" in migration.sql
    assert "projection_sha256" in migration.sql
    assert "11327" in migration.sql
    assert "22654" in migration.sql
    assert "events are append-only" in migration.sql
    assert "CCCT snapshot linked to a release generation is immutable" in migration.sql
    assert "validated generation law MST/article snapshots do not match manifest" in migration.sql
    assert "OLD.generation_id" in migration.sql
    assert "NEW.generation_id" in migration.sql
    assert "generation article snapshot does not match PostgreSQL ledger" in migration.sql
    assert "content         text NOT NULL" in migration.sql
    assert "pg_advisory" not in migration.sql
    assert "building" in migration.sql
    assert "aborted" in migration.sql
    assert "validated" in migration.sql
    assert "sealed" in migration.sql
    assert "active" in migration.sql
    assert "retired" in migration.sql
