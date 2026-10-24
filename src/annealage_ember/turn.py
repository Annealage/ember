"""One finite turn as a job: how it ended, and teardown that finishes exactly once.

A hosted product runs a user turn (or one prompt) as a job whose only
publication is its exit. The agent may succeed, fail, or be interrupted (a
signal, a cancel, an idle expiry), and the three must never be confused: only
a turn that ran to completion, tore down cleanly and was not interrupted at any
point, even after its result was laid out, may publish.

``TurnOutcome`` is that verdict: ``SUCCESS``, ``FAILURE`` or ``CANCELLED``. Only
``SUCCESS`` has ``publishes``; the exit codes are ``0``, ``1`` and ``143`` (the
signal convention a launcher already reads as "nothing published").

``Teardown`` is the idempotent half. Steps run once, last registered first, each
isolated from the others' failures. ``run()`` may be called any number of times
and from any task (an outside cancel included): every call waits for the same
single run, and a cancel of the waiter never abandons it.

``TurnRun`` joins them for one job::

    run = TurnRun()                       # inside the job's main task
    run.teardown.add(stop_the_child)      # steps; the last added runs first
    outcome = await run.execute(work)     # work() returns, or is ended by complete()
    outcome.conclude()                    # SystemExit(1 / 143) unless it succeeded

A serving job (Canvas) ends ``work`` with ``run.complete()`` from a watcher, and a
signal handler cancels the job's task (``task.cancel``); ``execute`` can tell the
two apart, because the task counts cancel requests and ``complete()`` is the only
one it made itself. A finite prompt (Trace) lets ``work`` return and passes
``returns_complete=True``. Cancelled work is never resumed or retried here.

Interruption detection reads ``asyncio.Task.cancelling()``, so ``TurnRun`` needs
Python 3.11 or newer. Nothing here knows what a product publishes or what its
agent does; the product's teardown steps own that, and read
:attr:`TurnRun.may_publish` before laying anything out.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import sys
import traceback
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Tuple, Union

#: What a launcher reads as "stopped by a signal, nothing published".
CANCELLED_EXIT = 143
FAILED_EXIT = 1


class TurnStatus(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TurnOutcome:
    """How a turn ended. ``reason`` is operator-facing; ``error`` the first failure, if any."""

    status: TurnStatus
    reason: str = ""
    error: Optional[BaseException] = None

    @classmethod
    def success(cls) -> "TurnOutcome":
        return cls(TurnStatus.SUCCESS)

    @classmethod
    def failure(
        cls, reason: str = "turn failed; nothing published", error: Optional[BaseException] = None
    ):
        return cls(TurnStatus.FAILURE, reason, error)

    @classmethod
    def cancelled(cls, reason: str = "turn interrupted; nothing published") -> "TurnOutcome":
        return cls(TurnStatus.CANCELLED, reason)

    @property
    def succeeded(self) -> bool:
        return self.status is TurnStatus.SUCCESS

    @property
    def publishes(self) -> bool:
        """Only a success signals publication; a cancelled or failed turn never does."""
        return self.status is TurnStatus.SUCCESS

    @property
    def exit_code(self) -> int:
        if self.status is TurnStatus.SUCCESS:
            return 0
        return CANCELLED_EXIT if self.status is TurnStatus.CANCELLED else FAILED_EXIT

    def conclude(self, stream=None) -> None:
        """Return for a success; otherwise say why on ``stream`` (stderr) and ``SystemExit``."""
        if self.succeeded:
            return
        print(self.reason, file=sys.stderr if stream is None else stream, flush=True)
        raise SystemExit(self.exit_code)


Step = Callable[[], Union[None, Awaitable[None]]]


class Teardown:
    """Cleanup steps that run exactly once, last registered first, whatever happens.

    A step is a callable (sync, or returning an awaitable) with no arguments. A step that
    raises is recorded in :attr:`errors`, reported to stderr, passed to ``on_error`` and
    never stops the steps after it. :meth:`run` is idempotent and cancel-resistant: later
    calls (concurrent or not) wait for the one run, and a cancel of a waiting task is
    absorbed, so the caller's own task still counts the request but teardown is never left
    half done.
    """

    def __init__(self, on_error: Optional[Callable[[BaseException], None]] = None):
        self._steps: List[Step] = []
        self._task: Optional["asyncio.Task[None]"] = None
        self.errors: List[BaseException] = []
        self.on_error = on_error

    def add(self, step: Step) -> Step:
        """Register ``step`` (returned, so it can decorate); refused once teardown has begun."""
        if self._task is not None:
            raise RuntimeError("teardown has already started")
        self._steps.append(step)
        return step

    @property
    def started(self) -> bool:
        return self._task is not None

    async def _drain(self) -> None:
        while self._steps:
            step = self._steps.pop()
            try:
                result = step()
                if inspect.isawaitable(result):
                    await result
            except (Exception, asyncio.CancelledError) as exc:
                # CancelledError here is a step awaiting something that was cancelled; the
                # drain task itself is never cancelled from outside.
                self.errors.append(exc)
                traceback.print_exception(type(exc), exc, exc.__traceback__)
                if self.on_error is not None:
                    self.on_error(exc)

    async def run(self) -> Tuple[BaseException, ...]:
        """Run every step once (or wait for the run in progress); return the step errors."""
        if self._task is None:
            self._task = asyncio.ensure_future(self._drain())
        while True:
            try:
                await asyncio.shield(self._task)
                return tuple(self.errors)
            except asyncio.CancelledError:
                if self._task.cancelled():
                    return tuple(self.errors)  # the loop itself is going away
                # An outside cancel while waiting: the waiter's task keeps its cancel count
                # (``TurnRun.interrupted``), and teardown still finishes before anyone returns.


class TurnRun:
    """State and lifecycle of one finite turn in one job; create it inside the job's main task."""

    def __init__(self, task: Optional["asyncio.Task"] = None):
        task = asyncio.current_task() if task is None else task
        if task is None:
            raise RuntimeError("TurnRun must be created inside a running task")
        if not hasattr(task, "cancelling"):
            raise RuntimeError("TurnRun needs Python 3.11 or newer (Task.cancelling)")
        self._task = task
        self._completions = 0
        self._failure: Optional[BaseException] = None
        self._failed = False
        self.completed = False
        self.teardown = Teardown(on_error=self._record_failure)

    @property
    def interrupted(self) -> bool:
        """Whether anything other than :meth:`complete` asked this job's task to cancel.

        More cancel requests than the one :meth:`complete` made means an outside stop,
        even one that lands after the result was laid out or during teardown.
        """
        return self._task.cancelling() > self._completions

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def may_publish(self) -> bool:
        """False once the turn failed or was interrupted; teardown steps check this first."""
        return not (self._failed or self.interrupted)

    def _end(self) -> None:
        """Wake a serving ``work`` once, with a cancel this run counts as its own."""
        if self._completions or self.teardown.started or self._task.done():
            return
        self._completions += 1
        self._task.cancel()

    def complete(self) -> None:
        """The turn ran to its end: stop serving. Idempotent; a later interrupt still wins."""
        self.completed = True
        self._end()

    def fail(self, error: Union[BaseException, str, None] = None, *, stop: bool = False) -> None:
        """Record a failure (the first one is kept); ``stop`` also ends a serving ``work``."""
        if isinstance(error, BaseException):
            traceback.print_exception(type(error), error, error.__traceback__)
        self._record_failure(error)
        if stop:
            self._end()

    def _record_failure(self, error: Union[BaseException, str, None]) -> None:
        if isinstance(error, str):
            error = RuntimeError(error)
        if not self._failed:
            self._failed = True
            self._failure = error

    def outcome(self) -> TurnOutcome:
        """The verdict so far; an interrupt beats a failure beats an incomplete turn."""
        if self.interrupted:
            return TurnOutcome.cancelled()
        if self._failed:
            return TurnOutcome.failure(error=self._failure)
        if not self.completed:
            return TurnOutcome.cancelled("turn did not complete; nothing published")
        return TurnOutcome.success()

    async def execute(
        self, work: Callable[[], Awaitable[None]], *, returns_complete: bool = False
    ) -> TurnOutcome:
        """Await ``work()``, tear down to completion, and say how the turn ended.

        ``work`` ends by returning (success only with ``returns_complete``), by
        :meth:`complete` (the watcher's stop), by raising (a failure) or by being
        cancelled (an interrupt unless it was :meth:`complete`'s). Teardown runs on every
        path, once, even for ``KeyboardInterrupt``/``SystemExit``, which still propagate.
        """
        try:
            await work()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.fail(exc)
        else:
            if returns_complete:
                self.completed = True
        finally:
            await self.teardown.run()
        return self.outcome()
