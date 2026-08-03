from __future__ import annotations

import json
import math
import multiprocessing
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from src import model_runtime as model_runtime_module
from src import neo4j_runtime as neo4j_runtime_module
from src import process_ipc as process_ipc_module
from src.db import postgres as postgres_module
from src.db import qdrant as qdrant_module
from src.deadline import DeadlineExceeded, RequestDeadline
from src.model_runtime import (
    DenseModelSupervisor,
    ModelInferenceTimeout,
    ModelRuntimeError,
)
from src.neo4j_runtime import (
    KillableNeo4jQueryExecutor,
    Neo4jQueryTimeout,
    Neo4jRuntimeError,
)
from src.process_ipc import (
    file_identity,
    read_bounded_path,
    write_bounded_response,
)
from src.process_start import start_process_bounded
from src.production_settings import (
    ProductionConfigurationError,
    ProductionSettings,
    validate_runtime_deadline_settings,
)

_SENSITIVE_CANARIES = {
    "EVIDENCE_SERVICE_TOKEN": "must-not-enter-child",
    "ANTHROPIC_API_KEY": "must-not-enter-child",
    "POSTGRES_PASSWORD": "must-not-enter-child",
    "LAW_API_OC": "must-not-enter-child",
}


def _environment_is_scrubbed() -> bool:
    return all(name not in os.environ for name in _SENSITIVE_CANARIES)


def _read_model_control(connection: Any) -> tuple[dict[str, Any], list[str]]:
    control = json.loads(connection.recv_bytes(2_048).decode("utf-8"))
    request = json.loads(
        read_bounded_path(
            control["request_path"],
            expected_identity=tuple(control["request_identity"]),
            maximum_bytes=control["maximum_request_bytes"],
        ).decode("utf-8")
    )
    return control, request["texts"]


def _model_success_worker(connection: Any) -> None:
    environment_clean = _environment_is_scrubbed()
    connection.send(("ready",))
    try:
        while True:
            control, texts = _read_model_control(connection)
            assert len(texts) == 1
            assert control["command"] == "encode"
            vector = [0.0] * 1_024
            vector[0] = 1.0 if environment_clean else -1.0
            payload = json.dumps(
                {"status": "result", "vectors": [vector]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            write_bounded_response(
                control["response_path"],
                expected_identity=tuple(control["response_identity"]),
                payload=payload,
                maximum_bytes=control["maximum_response_bytes"],
            )
            connection.send(("result_ready", control["request_id"]))
    finally:
        connection.close()


def _model_hang_worker(connection: Any) -> None:
    connection.send(("ready",))
    _read_model_control(connection)
    while True:
        time.sleep(1)


def _model_ready_never_recv_worker(connection: Any) -> None:
    connection.send(("ready",))
    while True:
        time.sleep(1)


def _model_malformed_worker(connection: Any) -> None:
    connection.send(("ready",))
    control, _texts = _read_model_control(connection)
    payload = b'{"status":"result","vectors":[[0.0]]}'
    write_bounded_response(
        control["response_path"],
        expected_identity=tuple(control["response_identity"]),
        payload=payload,
        maximum_bytes=control["maximum_response_bytes"],
    )
    connection.send(("result_ready", control["request_id"]))
    while True:
        time.sleep(1)


def _neo_success_worker(**kwargs: Any) -> None:
    assert "parameters" not in kwargs
    parameters = json.loads(kwargs["parameters_json"].decode())
    payload = json.dumps(
        {
            "rows": [
                {
                    "environment_clean": _environment_is_scrubbed(),
                    "parameter": parameters["value"],
                }
            ],
            "status": "result",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    write_bounded_response(
        kwargs["response_path"],
        expected_identity=kwargs["response_identity"],
        payload=payload,
        maximum_bytes=kwargs["maximum_response_bytes"],
    )


def _neo_hang_worker(**_kwargs: Any) -> None:
    while True:
        time.sleep(1)


def _neo_abnormal_worker(**_kwargs: Any) -> None:
    os._exit(23)


def _deadline(
    *,
    request: float = 3.0,
    store: float = 1.0,
    model: float = 1.0,
    reserve: float = 0.1,
) -> RequestDeadline:
    return RequestDeadline.start(
        started_at=time.monotonic(),
        request_timeout_seconds=request,
        store_timeout_seconds=store,
        model_timeout_seconds=model,
        completion_reserve_seconds=reserve,
    )


def _child_pids() -> set[int]:
    return {process.pid for process in multiprocessing.active_children() if process.pid is not None}


def _temp_names(prefix: str) -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob(f"{prefix}*.json"))


def test_request_deadline_fake_clock_floor_and_libpq_minimum() -> None:
    now = [100.0]
    deadline = RequestDeadline.start(
        started_at=100.0,
        request_timeout_seconds=5.0,
        store_timeout_seconds=3.7,
        model_timeout_seconds=2.0,
        completion_reserve_seconds=0.25,
        clock=lambda: now[0],
    )
    assert deadline.remaining_seconds() == pytest.approx(5.0)
    assert deadline.work_seconds() == pytest.approx(4.75)
    assert deadline.store_operation_whole_seconds() == 3
    assert deadline.postgres_connect_whole_seconds() == 3

    now[0] = 102.8
    assert deadline.store_operation_whole_seconds() == 1
    with pytest.raises(DeadlineExceeded):
        deadline.postgres_connect_whole_seconds()

    now[0] = 104.76
    with pytest.raises(DeadlineExceeded):
        deadline.assert_work_allowed()


def test_runtime_settings_require_kill_reserves_inside_deadline() -> None:
    token = "deadline-test-token-" + ("x" * 40)
    invalid = ProductionSettings(
        _env_file=None,
        service_token=token,
        transport_mode="direct_tls",
        request_deadline_seconds=5,
        store_operation_timeout_seconds=3,
        store_termination_timeout_seconds=1,
        model_inference_timeout_seconds=1,
        model_termination_timeout_seconds=1,
        shutdown_grace_seconds=5,
    )
    with pytest.raises(ProductionConfigurationError, match="store operation"):
        validate_runtime_deadline_settings(invalid)


class _FastPostgresDeadline:
    completion_reserve_seconds = 0.1

    @staticmethod
    def postgres_connect_whole_seconds() -> int:
        return 2

    @staticmethod
    def store_operation_milliseconds() -> int:
        return 100

    @staticmethod
    def store_operation_seconds() -> float:
        return 0.1

    @staticmethod
    def assert_work_allowed() -> None:
        return None


class _BlackholePostgresConnection:
    def __init__(self) -> None:
        self.closed = False
        self.cancelled = threading.Event()
        self.released = threading.Event()
        self.rollback_calls = 0
        self.commit_calls = 0
        self.close_calls = 0

    def cancel_safe(self, *, timeout: float) -> None:
        assert 0 < timeout <= 0.1
        self.cancelled.set()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.released.set()

    def rollback(self) -> None:
        self.rollback_calls += 1

    def commit(self) -> None:
        self.commit_calls += 1


def test_postgres_blackhole_is_cancelled_closed_and_watchdog_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _BlackholePostgresConnection()
    captured: dict[str, Any] = {}

    def fake_connect(dsn: str, **kwargs: Any) -> _BlackholePostgresConnection:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 170000)
    monkeypatch.setattr(postgres_module.psycopg, "connect", fake_connect)
    monkeypatch.setattr(postgres_module.settings, "postgres_host", "db.internal")
    monkeypatch.setattr(postgres_module.settings, "postgres_hostaddr", "192.0.2.20")

    started = time.monotonic()
    with pytest.raises(DeadlineExceeded, match="PostgreSQL operation"):
        with postgres_module.connect(deadline=_FastPostgresDeadline()) as selected:
            assert selected is connection
            assert connection.released.wait(timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert connection.cancelled.is_set()
    assert connection.closed
    assert connection.close_calls >= 1
    assert captured["kwargs"]["connect_timeout"] == 2
    assert captured["kwargs"]["hostaddr"] == "192.0.2.20"
    assert "statement_timeout=100" in captured["kwargs"]["options"]
    assert not any(
        thread.name == "customs-evidence-postgres-deadline" for thread in threading.enumerate()
    )


def test_postgres_watchdog_firing_during_commit_cannot_return_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CommitRaceConnection(_BlackholePostgresConnection):
        def commit(self) -> None:
            self.commit_calls += 1
            assert self.released.wait(timeout=1)

    connection = CommitRaceConnection()
    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 170000)
    monkeypatch.setattr(
        postgres_module.psycopg,
        "connect",
        lambda _dsn, **_kwargs: connection,
    )
    monkeypatch.setattr(postgres_module.settings, "postgres_host", "db.internal")
    monkeypatch.setattr(postgres_module.settings, "postgres_hostaddr", "192.0.2.20")

    with pytest.raises(DeadlineExceeded, match="commit crossed"):
        with postgres_module.connect(deadline=_FastPostgresDeadline()):
            pass

    assert connection.commit_calls == 1
    assert connection.cancelled.is_set()
    assert connection.closed
    assert not any(
        thread.name == "customs-evidence-postgres-deadline" for thread in threading.enumerate()
    )


def test_postgres_post_connect_setup_failure_closes_without_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _BlackholePostgresConnection()

    class RejectAfterConnect(_FastPostgresDeadline):
        @staticmethod
        def assert_work_allowed() -> None:
            raise DeadlineExceeded("reject after connect")

    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 170000)
    monkeypatch.setattr(
        postgres_module.psycopg,
        "connect",
        lambda _dsn, **_kwargs: connection,
    )
    monkeypatch.setattr(postgres_module.settings, "postgres_host", "db.internal")
    monkeypatch.setattr(postgres_module.settings, "postgres_hostaddr", "192.0.2.20")

    with pytest.raises(DeadlineExceeded, match="reject after connect"):
        with postgres_module.connect(deadline=RejectAfterConnect()):
            pytest.fail("setup failure must happen before yielding the connection")

    assert connection.closed
    assert connection.close_calls == 1
    assert not any(
        thread.name == "customs-evidence-postgres-deadline" for thread in threading.enumerate()
    )


def test_postgres_deadline_runtime_rejects_old_libpq_and_dns_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 169999)
    with pytest.raises(RuntimeError, match="libpq 17"):
        postgres_module.validate_deadline_runtime()

    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 170000)
    monkeypatch.setattr(postgres_module.settings, "postgres_host", "db.internal")
    monkeypatch.setattr(postgres_module.settings, "postgres_hostaddr", "")
    with pytest.raises(RuntimeError, match="hostaddr"):
        postgres_module.validate_deadline_runtime()


class _BlackholeQdrant:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.close_calls = 0

    def slow_drip_forever(self) -> None:
        assert self.closed.wait(timeout=3)
        raise TimeoutError("transport closed by wall-clock watchdog")

    def ignore_transport_error_and_return(self) -> None:
        assert self.closed.wait(timeout=3)

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


def test_qdrant_request_client_disables_probe_and_absolute_watchdog_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor: dict[str, Any] = {}

    def fake_qdrant_client(**kwargs: Any) -> object:
        constructor.update(kwargs)
        return object()

    monkeypatch.setattr(qdrant_module, "QdrantClient", fake_qdrant_client)
    qdrant_module.client(timeout_seconds=2)
    assert constructor["timeout"] == 2
    assert constructor["check_compatibility"] is False

    instance = _BlackholeQdrant()
    captured_timeout: list[int] = []

    def factory(*, timeout_seconds: int) -> _BlackholeQdrant:
        captured_timeout.append(timeout_seconds)
        return instance

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="wall-clock watchdog"):
        with qdrant_module.bounded_client(
            _deadline(request=3, store=1),
            client_factory=factory,
        ) as client:
            client.slow_drip_forever()
    elapsed = time.monotonic() - started

    assert 0.8 <= elapsed < 1.8
    assert captured_timeout == [1]
    assert instance.close_calls == 1
    assert not any(
        thread.name == "customs-evidence-qdrant-deadline" for thread in threading.enumerate()
    )

    ignored = _BlackholeQdrant()
    with pytest.raises(DeadlineExceeded, match="Qdrant operation"):
        with qdrant_module.bounded_client(
            _deadline(request=3, store=1),
            client_factory=lambda *, timeout_seconds: ignored,
        ) as client:
            client.ignore_transport_error_and_return()


def test_qdrant_post_construction_setup_failure_closes_client_once() -> None:
    instance = _BlackholeQdrant()

    class RejectTimerSetup:
        completion_reserve_seconds = 0.1

        @staticmethod
        def store_operation_whole_seconds() -> int:
            return 1

        @staticmethod
        def store_operation_seconds() -> float:
            raise DeadlineExceeded("reject watchdog setup")

    with pytest.raises(DeadlineExceeded, match="reject watchdog setup"):
        with qdrant_module.bounded_client(
            RejectTimerSetup(),  # type: ignore[arg-type]
            client_factory=lambda *, timeout_seconds: instance,
        ):
            pytest.fail("setup failure must happen before yielding the client")

    assert instance.close_calls == 1
    assert not any(
        thread.name == "customs-evidence-qdrant-deadline" for thread in threading.enumerate()
    )


def _neo_executor(
    worker: Any,
    *,
    maximum_request_bytes: int = 1_048_576,
) -> KillableNeo4jQueryExecutor:
    def unexpected_fatal_exit(code: int) -> Any:
        pytest.fail(f"unexpected Neo4j test-process fatal exit {code}")

    return KillableNeo4jQueryExecutor(
        uri="bolt://127.0.0.1:7687",
        user="explicit-user",
        password="explicit-password",
        # Process startup and reaping on shared Linux CI runners can exceed
        # 100 ms without violating the production shutdown contract.
        termination_timeout_seconds=0.25,
        maximum_request_bytes=maximum_request_bytes,
        worker_target=worker,
        fatal_exit=unexpected_fatal_exit,
    )


def test_neo4j_streaming_result_preserves_canonical_wire_without_full_list() -> None:
    descriptor, name = tempfile.mkstemp(prefix="neo4j-stream-test-", suffix=".json")
    identity = file_identity(descriptor)
    consumed: list[int] = []

    def rows():
        for index in range(1_000):
            consumed.append(index)
            yield {"value": index, "label": f"row-{index}"}

    try:
        neo4j_runtime_module._write_streaming_result(
            name,
            expected_identity=identity,
            rows=rows(),
            maximum_rows=1_000,
            maximum_bytes=1_048_576,
        )
        payload = json.loads(
            read_bounded_path(
                name,
                expected_identity=identity,
                maximum_bytes=1_048_576,
            ).decode("utf-8")
        )
    finally:
        os.close(descriptor)
        os.unlink(name)

    assert consumed == list(range(1_000))
    assert payload["status"] == "result"
    assert payload["rows"][0] == {"label": "row-0", "value": 0}
    assert payload["rows"][-1] == {"label": "row-999", "value": 999}


def test_neo4j_canonical_ipc_scrubs_environment_and_returns_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _SENSITIVE_CANARIES.items():
        monkeypatch.setenv(name, value)
    executor = _neo_executor(_neo_success_worker)
    try:
        rows = executor.run(
            "RETURN $value AS value",
            {"value": 17},
            _deadline(request=10, store=5),
        )
    finally:
        executor.close()
    assert rows == [{"environment_clean": True, "parameter": 17}]
    assert executor.active_process_count == 0


def test_neo4j_hang_is_terminated_reaped_and_temp_removed() -> None:
    before_children = _child_pids()
    before_temp = _temp_names("customs-evidence-neo4j-")
    executor = _neo_executor(_neo_hang_worker)
    started = time.monotonic()
    try:
        with pytest.raises(Neo4jQueryTimeout):
            executor.run(
                "RETURN 1",
                {},
                _deadline(request=2, store=0.2),
            )
    finally:
        executor.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert executor.active_process_count == 0
    assert _child_pids() <= before_children
    assert _temp_names("customs-evidence-neo4j-") == before_temp


def test_neo4j_close_kills_inflight_child_and_recovers_capacity() -> None:
    executor = _neo_executor(_neo_hang_worker)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            executor.run(
                "RETURN 1",
                {},
                _deadline(request=10, store=5),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke, name="neo4j-deadline-test")
    thread.start()
    cutoff = time.monotonic() + 2
    while executor.active_process_count != 1 and time.monotonic() < cutoff:
        time.sleep(0.01)
    assert executor.active_process_count == 1

    started = time.monotonic()
    executor.close()
    thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not thread.is_alive()
    assert errors
    assert executor.active_process_count == 0
    with pytest.raises(Neo4jRuntimeError, match="closed"):
        executor.run("RETURN 1", {}, _deadline())


def test_neo4j_close_reaps_two_permanent_hangs_in_one_shutdown_window() -> None:
    before_children = _child_pids()
    before_temp = _temp_names("customs-evidence-neo4j-")
    executor = _neo_executor(_neo_hang_worker)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            executor.run(
                "RETURN 1",
                {},
                _deadline(request=10, store=5),
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, name=f"neo4j-concurrency-test-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    cutoff = time.monotonic() + 5
    while executor.active_process_count != 2 and time.monotonic() < cutoff:
        time.sleep(0.01)
    assert executor.active_process_count == 2

    started = time.monotonic()
    executor.close()
    for thread in threads:
        thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert all(not thread.is_alive() for thread in threads)
    assert len(errors) == 2
    assert executor.active_process_count == 0
    assert _child_pids() <= before_children
    assert _temp_names("customs-evidence-neo4j-") == before_temp


def test_neo4j_shutdown_cost_is_constant_for_one_and_eight_slow_children() -> None:
    termination_timeout = 0.08

    def measure(child_count: int) -> float:
        executor = KillableNeo4jQueryExecutor(
            uri="bolt://127.0.0.1:7687",
            user="explicit-user",
            password="explicit-password",
            termination_timeout_seconds=termination_timeout,
            worker_target=_neo_success_worker,
            fatal_exit=lambda code: pytest.fail(f"unexpected fatal exit {code}"),
        )

        class SlowTerminateThenKillProcess:
            def __init__(self) -> None:
                self.alive = True
                self.killed = False
                self.terminate_calls = 0
                self.kill_calls = 0

            def is_alive(self) -> bool:
                return self.alive

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.kill_calls += 1
                self.killed = True

            def join(self, timeout: float) -> None:
                time.sleep(timeout)
                if self.killed:
                    self.alive = False
                    executor._unregister(self)

        processes = [SlowTerminateThenKillProcess() for _index in range(child_count)]
        for process in processes:
            executor._register(process)

        started = time.monotonic()
        executor.close()
        elapsed = time.monotonic() - started

        assert executor.active_process_count == 0
        assert all(not process.alive for process in processes)
        assert all(process.terminate_calls == 1 for process in processes)
        assert all(process.kill_calls == 1 for process in processes)
        return elapsed

    one_child = measure(1)
    eight_children = measure(8)
    upper_bound = 3 * termination_timeout + 0.15

    assert 1.5 * termination_timeout <= one_child <= upper_bound
    assert 1.5 * termination_timeout <= eight_children <= upper_bound
    assert eight_children <= one_child + termination_timeout + 0.15


def test_neo4j_rejects_combined_query_parameter_limit_and_abnormal_exit() -> None:
    limited = _neo_executor(_neo_success_worker, maximum_request_bytes=20)
    try:
        with pytest.raises(Neo4jRuntimeError, match="IPC byte limit"):
            limited.run("RETURN $value", {"value": 1}, _deadline())
    finally:
        limited.close()

    abnormal = _neo_executor(_neo_abnormal_worker)
    try:
        with pytest.raises(Neo4jRuntimeError, match="abnormally"):
            abnormal.run("RETURN 1", {}, _deadline(request=10, store=5))
    finally:
        abnormal.close()


def test_neo4j_setup_time_is_not_reset_before_query_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def delayed_start(process: Any, **kwargs: Any) -> None:
        time.sleep(float(kwargs["timeout_seconds"]) + 0.05)
        process.start()

    monkeypatch.setattr(
        neo4j_runtime_module,
        "start_process_bounded",
        delayed_start,
    )
    executor = _neo_executor(_neo_success_worker)
    started = time.monotonic()
    try:
        with pytest.raises(Neo4jQueryTimeout):
            executor.run(
                "RETURN 1",
                {"value": 1},
                _deadline(request=2, store=0.2),
            )
    finally:
        executor.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert executor.active_process_count == 0


def test_neo4j_partial_temp_setup_failure_closes_and_unlinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_temp = _temp_names("customs-evidence-neo4j-")
    executor = _neo_executor(_neo_success_worker)

    def fail_identity(_descriptor: int) -> tuple[int, int]:
        raise OSError("synthetic identity failure")

    monkeypatch.setattr(
        neo4j_runtime_module,
        "file_identity",
        fail_identity,
    )
    try:
        with pytest.raises(OSError, match="synthetic identity failure"):
            executor.run(
                "RETURN 1",
                {"value": 1},
                _deadline(request=2, store=0.5),
            )
    finally:
        executor.close()

    assert executor.active_process_count == 0
    assert _temp_names("customs-evidence-neo4j-") == before_temp


def test_neo4j_partial_process_start_is_reaped_even_when_start_raises() -> None:
    class SyntheticFatalExit(RuntimeError):
        pass

    class PartialStartProcess:
        def __init__(self) -> None:
            self.alive = False
            self.terminated = False
            self.closed = False
            self.exitcode: int | None = None

        def start(self) -> None:
            self.alive = True
            raise OSError("synthetic post-child start failure")

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False
            self.exitcode = -15

        def kill(self) -> None:
            self.alive = False
            self.exitcode = -9

        def join(self, _timeout: float) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class PartialStartContext:
        def __init__(self) -> None:
            self.process: PartialStartProcess | None = None

        def Process(self, **_kwargs: Any) -> PartialStartProcess:  # noqa: N802
            self.process = PartialStartProcess()
            return self.process

    fatal_codes: list[int] = []

    def fatal_exit(code: int) -> Any:
        fatal_codes.append(code)
        raise SyntheticFatalExit("service must fail-stop")

    context = PartialStartContext()
    executor = KillableNeo4jQueryExecutor(
        uri="bolt://127.0.0.1:7687",
        user="explicit-user",
        password="explicit-password",
        termination_timeout_seconds=0.1,
        process_context=context,  # type: ignore[arg-type]
        worker_target=_neo_success_worker,
        fatal_exit=fatal_exit,
    )
    before_temp = _temp_names("customs-evidence-neo4j-")
    with pytest.raises(SyntheticFatalExit, match="fail-stop"):
        executor.run(
            "RETURN 1",
            {"value": 1},
            _deadline(request=2, store=0.5),
        )
    executor.close()

    assert fatal_codes == [72]
    assert context.process is not None
    assert context.process.terminated
    assert context.process.closed
    assert not context.process.alive
    assert executor.active_process_count == 0
    assert _temp_names("customs-evidence-neo4j-") == before_temp
    assert not any(
        thread.name == "customs-evidence-neo4j-launcher" for thread in threading.enumerate()
    )


def _model_supervisor(worker: Any) -> DenseModelSupervisor:
    return DenseModelSupervisor(
        startup_timeout_seconds=15,
        termination_timeout_seconds=0.1,
        worker_target=worker,
    )


def test_model_partial_process_construction_failure_closes_pipe_endpoints() -> None:
    class FailingProcessContext:
        def __init__(self) -> None:
            self.parent: Any | None = None
            self.child: Any | None = None

        def Pipe(self, *, duplex: bool) -> tuple[Any, Any]:  # noqa: N802
            self.parent, self.child = multiprocessing.get_context("spawn").Pipe(duplex=duplex)
            return self.parent, self.child

        @staticmethod
        def Process(**_kwargs: Any) -> Any:  # noqa: N802
            raise OSError("synthetic process construction failure")

    context = FailingProcessContext()
    supervisor = DenseModelSupervisor(
        startup_timeout_seconds=1,
        termination_timeout_seconds=0.1,
        worker_target=_model_success_worker,
        process_context=context,  # type: ignore[arg-type]
    )
    with pytest.raises(ModelRuntimeError, match="startup failed"):
        supervisor.start()

    assert context.parent is not None and context.parent.closed
    assert context.child is not None and context.child.closed
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.assert_ready()
    supervisor.close()


def test_model_success_validates_shape_scrubs_environment_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _SENSITIVE_CANARIES.items():
        monkeypatch.setenv(name, value)
    before_children = _child_pids()
    supervisor = _model_supervisor(_model_success_worker)
    supervisor.start()
    vector = supervisor.encode(
        ["bounded model request"],
        _deadline(request=2, model=0.5),
    )
    assert len(vector) == 1
    assert len(vector[0]) == 1_024
    assert vector[0][0] == 1.0
    assert all(math.isfinite(value) for value in vector[0])
    supervisor.close()
    assert _child_pids() <= before_children


def test_model_hang_is_killed_reaped_broken_and_temp_removed() -> None:
    before_children = _child_pids()
    before_temp = _temp_names("customs-evidence-model-")
    supervisor = _model_supervisor(_model_hang_worker)
    supervisor.start()
    started = time.monotonic()
    with pytest.raises(ModelInferenceTimeout):
        supervisor.encode(
            ["hang"],
            _deadline(request=2, model=0.2),
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.assert_ready()
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.encode(["second"], _deadline())
    supervisor.close()
    assert _child_pids() <= before_children
    assert _temp_names("customs-evidence-model-") == before_temp


def test_model_maximum_korean_input_cannot_block_pipe_before_child_recv() -> None:
    before_children = _child_pids()
    before_temp = _temp_names("customs-evidence-model-")
    supervisor = _model_supervisor(_model_ready_never_recv_worker)
    supervisor.start()
    started = time.monotonic()
    with pytest.raises(ModelInferenceTimeout):
        supervisor.encode(
            ["가" * 32_768],
            _deadline(request=2, model=0.2),
        )
    elapsed = time.monotonic() - started

    supervisor.close()
    assert elapsed < 1.0
    assert _child_pids() <= before_children
    assert _temp_names("customs-evidence-model-") == before_temp
    assert not any(
        thread.name == "customs-evidence-model-launcher" for thread in threading.enumerate()
    )


def test_model_partial_temp_setup_failure_cleans_request_and_keeps_worker_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_temp = _temp_names("customs-evidence-model-")
    supervisor = _model_supervisor(_model_success_worker)
    supervisor.start()
    real_mkstemp = model_runtime_module.tempfile.mkstemp
    calls = 0

    def fail_second_temp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic response temp failure")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(model_runtime_module.tempfile, "mkstemp", fail_second_temp)
    with pytest.raises(ModelRuntimeError, match="IPC failed"):
        supervisor.encode(
            ["partial setup"],
            _deadline(request=2, model=0.5),
        )

    supervisor.assert_ready()
    supervisor.close()
    assert _temp_names("customs-evidence-model-") == before_temp


def test_model_temp_setup_consumes_the_same_absolute_subdeadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_temp = _temp_names("customs-evidence-model-")
    supervisor = _model_supervisor(_model_success_worker)
    supervisor.start()
    real_mkstemp = model_runtime_module.tempfile.mkstemp
    delayed = False

    def delayed_first_temp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        nonlocal delayed
        if not delayed:
            delayed = True
            time.sleep(0.25)
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(model_runtime_module.tempfile, "mkstemp", delayed_first_temp)
    started = time.monotonic()
    with pytest.raises(ModelInferenceTimeout):
        supervisor.encode(
            ["setup deadline"],
            _deadline(request=2, model=0.2),
        )
    elapsed = time.monotonic() - started

    supervisor.assert_ready()
    supervisor.close()
    assert 0.2 <= elapsed < 0.8
    assert _temp_names("customs-evidence-model-") == before_temp


def test_model_malformed_response_terminates_worker_and_fails_closed() -> None:
    supervisor = _model_supervisor(_model_malformed_worker)
    supervisor.start()
    with pytest.raises(ModelRuntimeError, match="invalid vectors"):
        supervisor.encode(
            ["malformed"],
            _deadline(request=2, model=0.5),
        )
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.assert_ready()
    supervisor.close()


def test_model_lock_wait_and_inference_share_one_subdeadline() -> None:
    supervisor = _model_supervisor(_model_hang_worker)
    supervisor.start()
    first_errors: list[BaseException] = []

    def first_call() -> None:
        try:
            supervisor.encode(
                ["first"],
                _deadline(request=3, model=0.8),
            )
        except BaseException as exc:
            first_errors.append(exc)

    thread = threading.Thread(target=first_call, name="model-deadline-test")
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    with pytest.raises(ModelInferenceTimeout, match="admission"):
        supervisor.encode(
            ["queued"],
            _deadline(request=2, model=0.2),
        )
    elapsed = time.monotonic() - started
    thread.join(timeout=2)
    supervisor.close()

    assert elapsed < 0.5
    assert not thread.is_alive()
    assert first_errors


def test_model_close_interrupts_concurrent_inference_without_lock_deadlock() -> None:
    supervisor = _model_supervisor(_model_hang_worker)
    supervisor.start()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            supervisor.encode(
                ["inflight"],
                _deadline(request=10, model=5),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke, name="model-close-test")
    thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    supervisor.close()
    thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not thread.is_alive()
    assert errors


def test_process_start_bounded_normal_path_leaves_no_launcher_thread() -> None:
    class ImmediateProcess:
        started = False

        def start(self) -> None:
            self.started = True

    process = ImmediateProcess()
    start_process_bounded(
        process,
        timeout_seconds=0.2,
        thread_name="bounded-start-normal-test",
        fatal_exit=lambda code: pytest.fail(f"unexpected fatal exit {code}"),
        fatal_exit_code=90,
    )

    assert process.started
    assert not any(thread.name == "bounded-start-normal-test" for thread in threading.enumerate())


def test_process_start_bounded_fail_stop_joins_injected_test_launcher() -> None:
    release = threading.Event()
    entered = threading.Event()
    fatal_codes: list[int] = []

    class SlowProcess:
        def start(self) -> None:
            entered.set()
            assert release.wait(timeout=1)

    class SyntheticFatalExit(RuntimeError):
        pass

    def fatal_exit(code: int) -> Any:
        fatal_codes.append(code)
        release.set()
        raise SyntheticFatalExit("service must fail-stop")

    with pytest.raises(SyntheticFatalExit, match="fail-stop"):
        start_process_bounded(
            SlowProcess(),
            timeout_seconds=0.05,
            thread_name="bounded-start-fatal-test",
            fatal_exit=fatal_exit,
            fatal_exit_code=91,
        )

    assert entered.is_set()
    assert fatal_codes == [91]
    assert not any(thread.name == "bounded-start-fatal-test" for thread in threading.enumerate())


def test_process_start_exception_after_child_creation_is_fail_stop() -> None:
    fatal_codes: list[int] = []

    class SyntheticFatalExit(RuntimeError):
        pass

    class PartialStartProcess:
        child_created = False

        def start(self) -> None:
            self.child_created = True
            raise OSError("synthetic post-child start failure")

    def fatal_exit(code: int) -> Any:
        fatal_codes.append(code)
        raise SyntheticFatalExit("partial start must fail-stop")

    process = PartialStartProcess()
    with pytest.raises(SyntheticFatalExit, match="partial start"):
        start_process_bounded(
            process,
            timeout_seconds=0.2,
            thread_name="bounded-start-partial-test",
            fatal_exit=fatal_exit,
            fatal_exit_code=92,
        )

    assert process.child_created
    assert fatal_codes == [92]
    assert not any(thread.name == "bounded-start-partial-test" for thread in threading.enumerate())


@pytest.mark.parametrize("advertise_nofollow", [False, True])
def test_ipc_path_open_handles_platform_nofollow_branches(
    monkeypatch: pytest.MonkeyPatch,
    advertise_nofollow: bool,
) -> None:
    descriptor, path = tempfile.mkstemp(prefix="customs-evidence-ipc-platform-")
    identity = file_identity(descriptor)
    real_open = process_ipc_module.os.open
    synthetic_nofollow = 0x2000_0000
    flags_seen: list[int] = []

    if advertise_nofollow:
        monkeypatch.setattr(
            process_ipc_module.os,
            "O_NOFOLLOW",
            synthetic_nofollow,
            raising=False,
        )
    else:
        monkeypatch.delattr(process_ipc_module.os, "O_NOFOLLOW", raising=False)

    def recording_open(selected_path: str, flags: int) -> int:
        flags_seen.append(flags)
        return real_open(selected_path, flags & ~synthetic_nofollow)

    monkeypatch.setattr(process_ipc_module.os, "open", recording_open)
    try:
        write_bounded_response(
            path,
            expected_identity=identity,
            payload=b'{"status":"ok"}',
            maximum_bytes=128,
        )
        assert (
            read_bounded_path(
                path,
                expected_identity=identity,
                maximum_bytes=128,
            )
            == b'{"status":"ok"}'
        )
    finally:
        os.close(descriptor)
        os.unlink(path)

    assert len(flags_seen) == 2
    assert all(bool(flags & synthetic_nofollow) is advertise_nofollow for flags in flags_seen)


def test_ipc_identity_is_checked_before_response_file_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_fd, response_path = tempfile.mkstemp(prefix="customs-evidence-ipc-response-")
    decoy_fd, decoy_path = tempfile.mkstemp(prefix="customs-evidence-ipc-decoy-")
    expected_identity = file_identity(response_fd)
    sentinel = b"must-not-be-truncated"
    os.write(decoy_fd, sentinel)
    real_open = process_ipc_module.os.open

    def swapped_open(selected_path: str, flags: int) -> int:
        if selected_path == response_path:
            return os.dup(decoy_fd)
        return real_open(selected_path, flags)

    monkeypatch.setattr(process_ipc_module.os, "open", swapped_open)
    try:
        with pytest.raises(RuntimeError, match="identity changed"):
            write_bounded_response(
                response_path,
                expected_identity=expected_identity,
                payload=b"replacement",
                maximum_bytes=128,
            )
        os.lseek(decoy_fd, 0, os.SEEK_SET)
        assert os.read(decoy_fd, len(sentinel) + 1) == sentinel
    finally:
        os.close(response_fd)
        os.close(decoy_fd)
        os.unlink(response_path)
        os.unlink(decoy_path)
