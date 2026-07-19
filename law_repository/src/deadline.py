"""One absolute request deadline shared by every production runtime dependency.

An HTTP timeout by itself does not stop synchronous driver or model work.  This
budget is passed into the PostgreSQL, Qdrant, Neo4j, and model boundaries so
each new blocking operation is capped by both:

* its configured per-operation maximum; and
* the time left before the service deadline, minus a completion reserve.

The reserve gives the blocking worker time to unwind and release admission
before the outer ASGI deadline is reached.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

REQUEST_DEADLINE_STATE_KEY = "_evidence_request_deadline"


class DeadlineExceeded(TimeoutError):
    """No safe blocking work may start within the remaining request budget."""


@dataclass(frozen=True, slots=True)
class RequestDeadline:
    """Absolute monotonic deadline plus bounded dependency-operation caps."""

    expires_at: float
    store_timeout_seconds: float
    model_timeout_seconds: float
    completion_reserve_seconds: float
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    @classmethod
    def start(
        cls,
        *,
        started_at: float,
        request_timeout_seconds: float,
        store_timeout_seconds: float,
        model_timeout_seconds: float,
        completion_reserve_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> RequestDeadline:
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if store_timeout_seconds <= 0 or model_timeout_seconds <= 0:
            raise ValueError("dependency timeout caps must be positive")
        if completion_reserve_seconds <= 0:
            raise ValueError("completion reserve must be positive")
        if completion_reserve_seconds >= request_timeout_seconds:
            raise ValueError("completion reserve must be shorter than request timeout")
        return cls(
            expires_at=started_at + request_timeout_seconds,
            store_timeout_seconds=store_timeout_seconds,
            model_timeout_seconds=model_timeout_seconds,
            completion_reserve_seconds=completion_reserve_seconds,
            _clock=clock,
        )

    def remaining_seconds(self) -> float:
        """Return wall-clock budget left before the public service deadline."""

        return max(0.0, self.expires_at - self._clock())

    def work_seconds(self) -> float:
        """Return time left before dependency work must already be unwinding."""

        return max(
            0.0,
            self.expires_at - self.completion_reserve_seconds - self._clock(),
        )

    def _operation_seconds(self, maximum_seconds: float, *, minimum: float) -> float:
        available = self.work_seconds()
        timeout = min(maximum_seconds, available)
        if not math.isfinite(timeout) or timeout < minimum:
            raise DeadlineExceeded("request dependency budget is exhausted")
        return timeout

    def store_operation_seconds(self, *, minimum: float = 0.001) -> float:
        return self._operation_seconds(self.store_timeout_seconds, minimum=minimum)

    def model_operation_seconds(self, *, minimum: float = 0.001) -> float:
        return self._operation_seconds(self.model_timeout_seconds, minimum=minimum)

    def store_operation_whole_seconds(self) -> int:
        """Return a libpq/Qdrant-compatible integer timeout.

        Those drivers expose integer-second controls for some operations.  We
        floor rather than ceil so the driver timeout can never cross the
        absolute work cutoff.  If less than one full second remains, the call
        is rejected before opening a socket.
        """

        timeout = math.floor(self.store_operation_seconds(minimum=1.0))
        if timeout < 1:
            raise DeadlineExceeded("less than one whole second remains for store I/O")
        return timeout

    def postgres_connect_whole_seconds(self) -> int:
        """Return a safe libpq connect timeout.

        libpq coerces values below two seconds up to two.  Starting a connect
        with less than two whole seconds would therefore violate the absolute
        budget instead of honoring it.
        """

        timeout = math.floor(self.store_operation_seconds(minimum=2.0))
        if timeout < 2:
            raise DeadlineExceeded("less than two whole seconds remain for PostgreSQL connect")
        return timeout

    def store_operation_milliseconds(self) -> int:
        timeout = math.floor(self.store_operation_seconds() * 1_000)
        if timeout < 1:
            raise DeadlineExceeded("less than one millisecond remains for store I/O")
        return timeout

    def assert_work_allowed(self) -> None:
        if self.work_seconds() <= 0:
            raise DeadlineExceeded("request dependency budget is exhausted")


__all__ = [
    "DeadlineExceeded",
    "REQUEST_DEADLINE_STATE_KEY",
    "RequestDeadline",
]
