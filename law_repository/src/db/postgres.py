"""Postgres 접속 헬퍼 (psycopg3)."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Timer

import psycopg

from src.config import settings
from src.deadline import DeadlineExceeded, RequestDeadline

_MIN_CANCEL_SAFE_LIBPQ_VERSION = 170000


def _deadline_hostaddr() -> str:
    configured = settings.postgres_hostaddr.strip()
    if configured:
        candidate = configured
    elif settings.postgres_host.lower() == "localhost":
        candidate = "127.0.0.1"
    else:
        candidate = settings.postgres_host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError as exc:
        raise RuntimeError(
            "production deadline enforcement requires one explicit PostgreSQL hostaddr"
        ) from exc


def validate_deadline_runtime() -> None:
    """Fail startup unless libpq supports bounded, thread-safe cancellation."""

    if psycopg.pq.version() < _MIN_CANCEL_SAFE_LIBPQ_VERSION:
        raise RuntimeError("production deadline enforcement requires libpq 17 or newer")
    _deadline_hostaddr()


@contextmanager
def connect(*, deadline: RequestDeadline | None = None) -> Iterator[psycopg.Connection]:
    """Open one transaction with driver and server-side hard time limits.

    Production callers pass one absolute request deadline.  libpq connection
    establishment, every SQL statement, lock wait, idle transaction, and the
    transaction as a whole are then bounded before the public HTTP deadline.
    The timer is a final client-side guard for a network path that stops
    delivering the server's ``statement_timeout`` response.
    """

    connect_kwargs: dict[str, object] = {}
    if deadline is not None:
        validate_deadline_runtime()
        connect_timeout = deadline.postgres_connect_whole_seconds()
        statement_timeout_ms = deadline.store_operation_milliseconds()
        connect_kwargs = {
            "connect_timeout": connect_timeout,
            # A single preconfigured address prevents request-time DNS and
            # libpq's per-address connect_timeout multiplication.
            "hostaddr": _deadline_hostaddr(),
            "options": (
                f"-c statement_timeout={statement_timeout_ms} "
                f"-c lock_timeout={statement_timeout_ms} "
                f"-c idle_in_transaction_session_timeout={statement_timeout_ms}"
            ),
        }
    conn: psycopg.Connection | None = None
    abort_timer: Timer | None = None
    timer_join_timeout = 0.0

    def stop_abort_timer() -> bool:
        nonlocal abort_timer
        timer = abort_timer
        if timer is None:
            return True
        timer.cancel()
        try:
            timer.join(timeout=timer_join_timeout)
        except RuntimeError:
            # ``Timer.start`` may fail before the thread is registered.
            pass
        quiesced = not timer.is_alive()
        abort_timer = None
        return quiesced

    try:
        connected = psycopg.connect(settings.postgres_dsn, **connect_kwargs)
        conn = connected
        # Keep every allocation and deadline calculation after a successful
        # connect inside this outer cleanup boundary.  Setup failures must not
        # strand a live connection before the context manager yields it.
        operation_expired = Event()
        if deadline is not None:
            deadline.assert_work_allowed()
            timer_join_timeout = deadline.completion_reserve_seconds

            def abort_at_request_cutoff() -> None:
                operation_expired.set()
                try:
                    connected.cancel_safe(
                        timeout=min(0.5, deadline.completion_reserve_seconds),
                    )
                # Closing the socket below is the mandatory fail-safe if the
                # cancellation packet cannot reach the failed network path.
                except Exception:  # noqa: S110  # nosec B110
                    pass
                finally:
                    connected.close()

            # Bound the whole transaction, not merely each statement.  This
            # also prevents a sequence of individually fast statements from
            # extending one dependency operation beyond its configured cap.
            abort_timer = Timer(
                deadline.store_operation_seconds(),
                abort_at_request_cutoff,
            )
            abort_timer.daemon = True
            abort_timer.name = "customs-evidence-postgres-deadline"
            abort_timer.start()
        try:
            yield connected
            if deadline is not None:
                if operation_expired.is_set():
                    raise DeadlineExceeded("PostgreSQL operation exceeded its hard deadline")
                deadline.assert_work_allowed()
            connected.commit()
            # A watchdog that fires while COMMIT is in progress can close the
            # socket yet race with a driver that reports commit success.
            # Quiesce the timer first, then make the final success decision.
            watchdog_quiesced = stop_abort_timer()
            if deadline is not None:
                if not watchdog_quiesced:
                    raise DeadlineExceeded("PostgreSQL deadline watchdog did not quiesce")
                if operation_expired.is_set():
                    raise DeadlineExceeded("PostgreSQL commit crossed its hard deadline")
                deadline.assert_work_allowed()
        except Exception:
            if not connected.closed:
                try:
                    connected.rollback()
                except Exception:
                    connected.close()
            raise
    finally:
        stop_abort_timer()
        if conn is not None:
            conn.close()
