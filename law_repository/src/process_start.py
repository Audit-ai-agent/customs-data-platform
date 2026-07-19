"""Fail-stop boundary for ``multiprocessing.Process.start``.

Python exposes no timeout argument for ``Process.start()``.  A request must
never return while a launcher thread may later create an untracked child.
Production therefore watches the local start call from a daemon helper thread
and terminates the whole service if that call does not finish by its absolute
budget.  The service supervisor is then responsible for a clean restart.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread
from typing import Any, NoReturn


class ProcessStartError(RuntimeError):
    """A child process could not be started normally."""


def start_process_bounded(
    process: Any,
    *,
    timeout_seconds: float,
    thread_name: str,
    fatal_exit: Callable[[int], NoReturn],
    fatal_exit_code: int,
) -> None:
    """Start ``process`` or fail-stop before an unbounded launcher can escape."""

    if timeout_seconds <= 0:
        raise ProcessStartError("process start budget is exhausted")
    finished = Event()
    errors: list[BaseException] = []

    def invoke_start() -> None:
        try:
            process.start()
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    launcher = Thread(
        target=invoke_start,
        name=thread_name,
        daemon=True,
    )
    launcher.start()
    if not finished.wait(timeout_seconds):
        # Returning or raising a recoverable request error here would leave the
        # launcher free to create a child later.  Production's implementation
        # is os._exit; injected test implementations may raise to prove this
        # branch without terminating the test runner.
        try:
            fatal_exit(fatal_exit_code)
        finally:
            # ``os._exit`` never reaches this block.  It exists so an injected
            # test fatal handler can release and deterministically join its
            # synthetic launcher instead of contaminating later tests.
            if finished.wait(timeout_seconds):
                launcher.join()
        raise ProcessStartError("fatal process-start exit unexpectedly returned")
    launcher.join()
    if errors:
        raise ProcessStartError("child process start failed") from errors[0]


__all__ = ["ProcessStartError", "start_process_bounded"]
