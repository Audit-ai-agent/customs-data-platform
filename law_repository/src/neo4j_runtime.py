"""Killable Neo4j query boundary for the production Evidence runtime.

Neo4j's transaction timeout is enforced by the server and its public Python
driver exposes connect, pool-acquisition, and write timeouts.  It does not
offer a general client read timeout for a connection that goes black-hole
after accepting a query.  Each production query therefore runs in a short
child process.  The parent kills and reaps that process before returning from
a timeout, so no Bolt thread or admission slot can leak indefinitely.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from multiprocessing.context import BaseContext
from threading import Condition, Lock, Thread
from typing import Any, NoReturn, Protocol

from src.deadline import DeadlineExceeded, RequestDeadline
from src.process_ipc import (
    FileIdentity,
    close_and_unlink,
    file_identity,
    read_bounded_response,
    write_bounded_response,
)
from src.process_security import scrub_sensitive_environment
from src.process_start import start_process_bounded


class Neo4jRuntimeError(RuntimeError):
    """A bounded Neo4j worker failed or returned an invalid response."""


class Neo4jQueryTimeout(DeadlineExceeded):
    """A Neo4j worker was killed at its hard client deadline."""


class Neo4jQueryExecutor(Protocol):
    def run(
        self,
        query: str,
        parameters: Mapping[str, Any],
        deadline: RequestDeadline,
    ) -> list[dict[str, Any]]: ...


def _write_streaming_result(
    path: str,
    *,
    expected_identity: FileIdentity,
    rows: Iterable[dict[str, Any]],
    maximum_rows: int,
    maximum_bytes: int,
) -> None:
    """Write a bounded result without retaining the full Neo4j payload twice.

    The parent still validates and decodes one bounded response.  Streaming in
    the child avoids the previous ``rows list -> giant JSON string`` peak,
    which could exhaust a constrained UAT host while the dense and reranker
    models were resident.
    """

    descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    written = 0

    def write_chunk(chunk: bytes) -> None:
        nonlocal written
        if not chunk or written + len(chunk) > maximum_bytes:
            raise ValueError("Neo4j response exceeds the byte limit")
        view = memoryview(chunk)
        offset = 0
        while offset < len(view):
            count = os.write(descriptor, view[offset:])
            if count <= 0:
                raise OSError("Neo4j response write made no progress")
            offset += count
        written += len(chunk)

    try:
        if file_identity(descriptor) != expected_identity:
            raise RuntimeError("child response file identity changed")
        os.ftruncate(descriptor, 0)
        write_chunk(b'{"rows":[')
        row_count = 0
        for row in rows:
            if row_count >= maximum_rows:
                raise ValueError("Neo4j query row limit exceeded")
            serialized = json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if row_count:
                write_chunk(b",")
            write_chunk(serialized)
            row_count += 1
        write_chunk(b'],"status":"result"}')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _neo4j_query_worker(
    *,
    uri: str,
    user: str,
    password: str,
    database: str | None,
    query: str,
    timeout_seconds: float,
    maximum_rows: int,
    parameters_json: bytes,
    response_path: str,
    response_identity: FileIdentity,
    maximum_response_bytes: int,
) -> None:
    try:
        from neo4j import GraphDatabase, Query

        parameters = json.loads(parameters_json.decode("utf-8"))
        if not isinstance(parameters, dict):
            raise ValueError("Neo4j parameters must be an object")
        driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=timeout_seconds,
            connection_acquisition_timeout=timeout_seconds,
            connection_write_timeout=timeout_seconds,
            liveness_check_timeout=timeout_seconds,
            max_connection_pool_size=1,
            keep_alive=False,
        )
        try:
            session_config: dict[str, Any] = {
                "connection_acquisition_timeout": timeout_seconds,
            }
            if database:
                session_config["database"] = database
            with driver.session(**session_config) as session:
                result = session.run(
                    Query(query, timeout=timeout_seconds),
                    **parameters,
                )
                _write_streaming_result(
                    response_path,
                    expected_identity=response_identity,
                    rows=(dict(record) for record in result),
                    maximum_rows=maximum_rows,
                    maximum_bytes=maximum_response_bytes,
                )
        finally:
            driver.close()
        return
    except BaseException as exc:
        payload = {"status": "error", "code": type(exc).__name__}
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (MemoryError, TypeError, ValueError):
        serialized = b'{"code":"serialization","status":"error"}'
    if len(serialized) > maximum_response_bytes:
        serialized = b'{"code":"response_size","status":"error"}'
    write_bounded_response(
        response_path,
        expected_identity=response_identity,
        payload=serialized,
        maximum_bytes=maximum_response_bytes,
    )


def _run_neo4j_worker_scrubbed(
    worker_target: Callable[..., None],
    worker_kwargs: dict[str, Any],
) -> None:
    scrub_sensitive_environment()
    worker_target(**worker_kwargs)


def _fatal_process_exit(code: int) -> NoReturn:
    os._exit(code)


class KillableNeo4jQueryExecutor:
    """Execute one Bolt query per child and reap it before every return."""

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        termination_timeout_seconds: float,
        database: str | None = None,
        maximum_rows: int = 100_000,
        maximum_request_bytes: int = 1_048_576,
        maximum_response_bytes: int = 4_194_304,
        start_method: str = "spawn",
        process_context: BaseContext | None = None,
        worker_target: Callable[..., None] = _neo4j_query_worker,
        fatal_exit: Callable[[int], NoReturn] = _fatal_process_exit,
    ) -> None:
        if termination_timeout_seconds <= 0:
            raise ValueError("Neo4j termination timeout must be positive")
        if maximum_rows <= 0:
            raise ValueError("Neo4j maximum row count must be positive")
        if maximum_request_bytes <= 0 or maximum_response_bytes <= 0:
            raise ValueError("Neo4j IPC byte limits must be positive")
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._termination_timeout = termination_timeout_seconds
        self._maximum_rows = maximum_rows
        self._maximum_request_bytes = maximum_request_bytes
        self._maximum_response_bytes = maximum_response_bytes
        self._context: Any = process_context or multiprocessing.get_context(start_method)
        self._worker_target = worker_target
        self._fatal_exit = fatal_exit
        self._condition = Condition()
        self._active_processes: dict[Any, Lock] = {}
        self._closed = False

    def _query_cutoff(self, deadline: RequestDeadline) -> float:
        cleanup_reserve = 2 * self._termination_timeout
        available = deadline.work_seconds() - cleanup_reserve
        timeout = min(deadline.store_timeout_seconds, available)
        if timeout < 0.001:
            raise Neo4jQueryTimeout("insufficient request budget for killable Neo4j query")
        return time.monotonic() + timeout

    @staticmethod
    def _is_alive(process: Any) -> bool:
        try:
            return bool(process.is_alive())
        except (AssertionError, ValueError):
            return False

    @staticmethod
    def _remaining(cutoff: float) -> float:
        remaining = cutoff - time.monotonic()
        if remaining < 0.001:
            raise Neo4jQueryTimeout("Neo4j query operation deadline is exhausted")
        return remaining

    def _terminate_and_reap(self, process: Any, process_lock: Lock) -> None:
        with process_lock:
            if self._is_alive(process):
                try:
                    process.terminate()
                except (AssertionError, OSError, ValueError):
                    pass
                try:
                    process.join(self._termination_timeout)
                except (AssertionError, OSError, ValueError):
                    pass
            if self._is_alive(process):
                try:
                    process.kill()
                except (AssertionError, OSError, ValueError):
                    pass
                try:
                    process.join(self._termination_timeout)
                except (AssertionError, OSError, ValueError):
                    pass
            if self._is_alive(process):
                self._fatal_exit(71)

    def _register(self, process: Any) -> Lock:
        process_lock = Lock()
        with self._condition:
            if self._closed:
                raise Neo4jRuntimeError("Neo4j runtime executor is closed")
            self._active_processes[process] = process_lock
        return process_lock

    def _unregister(self, process: Any) -> None:
        with self._condition:
            self._active_processes.pop(process, None)
            self._condition.notify_all()

    @property
    def active_process_count(self) -> int:
        with self._condition:
            return len(self._active_processes)

    def _start(
        self,
        process: Any,
        process_lock: Lock,
        operation_cutoff: float,
    ) -> None:
        # The same Process is serialized against shutdown; unrelated query
        # children retain independent locks and terminate in parallel.
        with process_lock:
            with self._condition:
                if self._closed:
                    raise Neo4jRuntimeError("Neo4j runtime executor is closed")
            start_process_bounded(
                process,
                timeout_seconds=self._remaining(operation_cutoff),
                thread_name="customs-evidence-neo4j-launcher",
                fatal_exit=self._fatal_exit,
                fatal_exit_code=72,
            )

    def _close_registered_children(
        self,
        active: tuple[tuple[Any, Lock], ...],
        *,
        shutdown_started: float,
    ) -> float:
        """Reap all children by shared phase cutoffs, independent of count."""

        terminate_cutoff = shutdown_started + self._termination_timeout
        kill_cutoff = terminate_cutoff + self._termination_timeout
        final_coordination_cutoff = kill_cutoff + self._termination_timeout

        def terminate_by_shared_cutoffs(process: Any, process_lock: Lock) -> None:
            with process_lock:
                if self._is_alive(process):
                    try:
                        process.terminate()
                    except (AssertionError, OSError, ValueError):
                        pass
                    try:
                        process.join(max(0.0, terminate_cutoff - time.monotonic()))
                    except (AssertionError, OSError, ValueError):
                        pass
                if self._is_alive(process):
                    try:
                        process.kill()
                    except (AssertionError, OSError, ValueError):
                        pass
                    try:
                        process.join(max(0.0, kill_cutoff - time.monotonic()))
                    except (AssertionError, OSError, ValueError):
                        pass
                if self._is_alive(process):
                    self._fatal_exit(78)

        terminators = [
            Thread(
                target=terminate_by_shared_cutoffs,
                args=(process, process_lock),
                name="customs-evidence-neo4j-shutdown",
                daemon=True,
            )
            for process, process_lock in active
        ]
        try:
            for thread in terminators:
                thread.start()
        except BaseException:
            # A partially launched shutdown fan-out cannot safely return while
            # some registered query child may survive.
            self._fatal_exit(77)
            raise
        for thread in terminators:
            thread.join(max(0.0, final_coordination_cutoff - time.monotonic()))
        if any(thread.is_alive() for thread in terminators):
            self._fatal_exit(73)
        if any(self._is_alive(process) for process, _lock in active):
            self._fatal_exit(74)
        return final_coordination_cutoff

    def run(
        self,
        query: str,
        parameters: Mapping[str, Any],
        deadline: RequestDeadline,
    ) -> list[dict[str, Any]]:
        if not query.strip() or len(query) > 100_000:
            raise Neo4jRuntimeError("Neo4j query is outside the bounded contract")
        try:
            parameters_json = json.dumps(
                dict(parameters),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise Neo4jRuntimeError("Neo4j parameters are not canonical JSON") from exc
        query_bytes = query.encode("utf-8")
        if len(query_bytes) + len(parameters_json) > self._maximum_request_bytes:
            raise Neo4jRuntimeError("Neo4j parameters exceed the IPC byte limit")
        operation_cutoff = self._query_cutoff(deadline)
        response_fd: int | None = None
        response_name: str | None = None
        process: Any | None = None
        registered = False
        process_lock: Lock | None = None
        try:
            response_fd, response_name = tempfile.mkstemp(
                prefix="customs-evidence-neo4j-",
                suffix=".json",
            )
            response_identity: FileIdentity = file_identity(response_fd)
            worker_kwargs = {
                "uri": self._uri,
                "user": self._user,
                "password": self._password,
                "database": self._database,
                "query": query,
                "timeout_seconds": self._remaining(operation_cutoff),
                "maximum_rows": self._maximum_rows,
                "parameters_json": parameters_json,
                "response_path": response_name,
                "response_identity": response_identity,
                "maximum_response_bytes": self._maximum_response_bytes,
            }
            process = self._context.Process(
                target=_run_neo4j_worker_scrubbed,
                args=(self._worker_target, worker_kwargs),
                name="customs-evidence-neo4j-query",
                daemon=True,
            )
            process_lock = self._register(process)
            registered = True
            self._start(process, process_lock, operation_cutoff)
            process.join(max(0.0, operation_cutoff - time.monotonic()))
            if self._is_alive(process):
                self._terminate_and_reap(process, process_lock)
                raise Neo4jQueryTimeout("Neo4j query exceeded its hard deadline")
            if process.exitcode != 0:
                raise Neo4jRuntimeError("Neo4j query worker exited abnormally")
            if time.monotonic() >= operation_cutoff:
                raise Neo4jQueryTimeout("Neo4j query exceeded its hard deadline")
            try:
                raw_response = read_bounded_response(
                    response_fd,
                    expected_identity=response_identity,
                    maximum_bytes=self._maximum_response_bytes,
                )
                message = json.loads(raw_response.decode("utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                raise Neo4jRuntimeError("Neo4j query worker response failed") from exc
            rows = message.get("rows") if isinstance(message, dict) else None
            if (
                not isinstance(message, dict)
                or set(message) != {"status", "rows"}
                or message.get("status") != "result"
                or not isinstance(rows, list)
            ):
                raise Neo4jRuntimeError("Neo4j query worker failed closed")
            if len(rows) > self._maximum_rows or any(not isinstance(row, dict) for row in rows):
                raise Neo4jRuntimeError("Neo4j query worker returned invalid rows")
            if time.monotonic() >= operation_cutoff:
                raise Neo4jQueryTimeout("Neo4j response processing exceeded its hard deadline")
            deadline.assert_work_allowed()
            return rows
        finally:
            if response_fd is not None and response_name is not None:
                close_and_unlink(response_fd, response_name)
            if process is not None and registered and process_lock is not None:
                try:
                    # Process.start can create the OS child and then raise
                    # during bootstrap.  Liveness, not a returned-success
                    # flag, decides whether cleanup is mandatory.
                    if self._is_alive(process):
                        self._terminate_and_reap(process, process_lock)
                finally:
                    self._unregister(process)
            if process is not None:
                try:
                    process.close()
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        """Stop accepting queries and kill every currently registered child."""

        shutdown_started = time.monotonic()
        with self._condition:
            self._closed = True
            active = tuple(self._active_processes.items())
        final_coordination_cutoff = self._close_registered_children(
            active,
            shutdown_started=shutdown_started,
        )
        # Query threads wake immediately after their child is reaped and own
        # final descriptor/temp cleanup.  The same 3*T shutdown cutoff covers
        # child termination, terminator bookkeeping, and registry cleanup.
        with self._condition:
            cleaned = self._condition.wait_for(
                lambda: not self._active_processes,
                timeout=max(0.0, final_coordination_cutoff - time.monotonic()),
            )
        if not cleaned:
            self._fatal_exit(75)


__all__ = [
    "KillableNeo4jQueryExecutor",
    "Neo4jQueryExecutor",
    "Neo4jQueryTimeout",
    "Neo4jRuntimeError",
]
