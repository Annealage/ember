"""``turn``: the outcome of one finite turn, and teardown that finishes exactly once."""

import asyncio
import sys

import pytest

from annealage_ember.turn import (
    CANCELLED_EXIT,
    FAILED_EXIT,
    Teardown,
    TurnOutcome,
    TurnRun,
    TurnStatus,
)

needs_cancelling = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="Task.cancelling needs 3.11"
)


def test_outcome_distinguishes_the_three_endings_and_only_success_publishes():
    ok, failed, cancelled = TurnOutcome.success(), TurnOutcome.failure(), TurnOutcome.cancelled()
    assert [o.status for o in (ok, failed, cancelled)] == [
        TurnStatus.SUCCESS,
        TurnStatus.FAILURE,
        TurnStatus.CANCELLED,
    ]
    assert [o.publishes for o in (ok, failed, cancelled)] == [True, False, False]
    assert (
        [o.exit_code for o in (ok, failed, cancelled)]
        == [0, FAILED_EXIT, CANCELLED_EXIT]
        == [0, 1, 143]
    )


def test_conclude_returns_for_success_and_exits_with_the_code_otherwise(capsys):
    TurnOutcome.success().conclude()
    for outcome in (TurnOutcome.failure(), TurnOutcome.cancelled()):
        with pytest.raises(SystemExit) as raised:
            outcome.conclude()
        assert raised.value.code == outcome.exit_code
    assert "nothing published" in capsys.readouterr().err


def test_teardown_runs_each_step_once_last_registered_first_and_is_idempotent():
    async def go():
        order = []
        teardown = Teardown()
        teardown.add(lambda: order.append("first"))

        async def second():
            await asyncio.sleep(0)
            order.append("second")

        teardown.add(second)
        results = await asyncio.gather(teardown.run(), teardown.run())
        await teardown.run()
        return order, results, teardown

    order, results, teardown = asyncio.run(go())
    assert order == ["second", "first"]
    assert results == [(), ()] and teardown.started
    with pytest.raises(RuntimeError):
        teardown.add(lambda: None)


def test_a_failing_step_is_recorded_and_does_not_stop_the_others(capsys):
    async def go():
        seen, reported = [], []
        teardown = Teardown(on_error=reported.append)
        teardown.add(lambda: seen.append("ran"))

        def broken():
            raise ValueError("boom")

        teardown.add(broken)
        errors = await teardown.run()
        return seen, errors, reported

    seen, errors, reported = asyncio.run(go())
    assert seen == ["ran"] and [str(e) for e in errors] == ["boom"] and reported == list(errors)
    assert "ValueError: boom" in capsys.readouterr().err


@needs_cancelling
def test_a_cancel_while_waiting_for_teardown_does_not_abandon_it():
    async def go():
        finished = []
        teardown = Teardown()

        async def slow():
            await asyncio.sleep(0.05)
            finished.append(True)

        teardown.add(slow)
        waiter = asyncio.ensure_future(teardown.run())
        await asyncio.sleep(0)
        waiter.cancel()
        await waiter  # absorbed: the waiter returns once teardown is done
        return finished, waiter.cancelling()

    finished, cancels = asyncio.run(go())
    assert finished == [True] and cancels == 1


def _run(outside=None, *, work=None, returns_complete=False, setup=None):
    """Drive ``TurnRun.execute``; ``outside(run, task)`` is the interleaving. -> (outcome, torn_down)."""

    async def go():
        torn_down = []

        async def serve():
            await asyncio.sleep(60)

        async def cleanup():
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            torn_down.append(True)

        task = asyncio.current_task()
        run = TurnRun()
        run.teardown.add(cleanup)
        if setup:
            setup(run)
        if outside:
            asyncio.get_running_loop().call_soon(outside, run, task)
        outcome = await run.execute(work or serve, returns_complete=returns_complete)
        return outcome, torn_down

    return asyncio.run(go())


@needs_cancelling
def test_complete_ends_serving_and_succeeds_after_teardown():
    outcome, torn_down = _run(lambda run, task: run.complete())
    assert outcome.succeeded and outcome.publishes and torn_down == [True]


@needs_cancelling
def test_an_outside_cancel_is_cancelled_even_after_complete_and_during_teardown():
    def both(run, task):
        run.complete()
        task.cancel()

    def late(run, task):
        run.complete()
        loop = asyncio.get_running_loop()
        loop.call_soon(lambda: loop.call_soon(task.cancel))

    for outside in (lambda run, task: task.cancel(), both, late):
        outcome, torn_down = _run(outside)
        assert outcome.status is TurnStatus.CANCELLED and not outcome.publishes
        assert outcome.exit_code == 143 and torn_down == [True]


@needs_cancelling
def test_a_failure_is_failure_not_cancelled_and_an_interrupt_still_wins():
    outcome, _ = _run(lambda run, task: run.complete(), setup=lambda run: run.fail("watcher broke"))
    assert outcome.status is TurnStatus.FAILURE and str(outcome.error) == "watcher broke"
    assert not outcome.publishes and outcome.exit_code == 1

    def fail_then_cancel(run, task):
        run.fail("late")
        task.cancel()

    outcome, _ = _run(fail_then_cancel)
    assert outcome.status is TurnStatus.CANCELLED


@needs_cancelling
def test_fail_with_stop_ends_a_serving_work():
    outcome, torn_down = _run(lambda run, task: run.fail("x", stop=True))
    assert outcome.status is TurnStatus.FAILURE and torn_down == [True]


@needs_cancelling
def test_work_raising_is_a_failure_and_still_tears_down(capsys):
    async def boom():
        raise OSError("socket")

    outcome, torn_down = _run(work=boom)
    assert outcome.status is TurnStatus.FAILURE and isinstance(outcome.error, OSError)
    assert torn_down == [True] and "OSError: socket" in capsys.readouterr().err


@needs_cancelling
def test_a_finite_prompt_returning_is_success_only_when_asked_and_otherwise_incomplete():
    async def prompt():
        await asyncio.sleep(0)

    outcome, _ = _run(work=prompt, returns_complete=True)
    assert outcome.succeeded
    outcome, _ = _run(work=prompt)
    assert outcome.status is TurnStatus.CANCELLED and "did not complete" in outcome.reason


@needs_cancelling
def test_a_failing_teardown_step_fails_the_turn_and_may_publish_goes_false():
    def broken(run):
        def step():
            raise RuntimeError("stop failed")

        run.teardown.add(step)

    outcome, torn_down = _run(lambda run, task: run.complete(), setup=broken)
    assert outcome.status is TurnStatus.FAILURE and torn_down == [True]


@needs_cancelling
def test_may_publish_is_false_after_an_interrupt_or_failure_seen_by_teardown_steps():
    seen = []

    def watch(run):
        run.teardown.add(lambda: seen.append(run.may_publish))

    _run(lambda run, task: run.complete(), setup=watch)
    _run(lambda run, task: task.cancel(), setup=watch)
    _run(lambda run, task: run.fail("x", stop=True), setup=watch)
    assert seen == [True, False, False]


def test_turn_run_outside_a_task_is_refused():
    with pytest.raises(RuntimeError):
        TurnRun()


@needs_cancelling
def test_a_turn_is_never_replayed_and_a_late_complete_cannot_make_it_succeed():
    async def main():
        run = TurnRun()
        calls = []

        async def work():
            calls.append(1)

        # teardown began (a shutdown path) before the watcher said complete
        await run.teardown.run()
        run.complete()
        assert not run.completed
        outcome = await run.execute(work)
        assert outcome.status is TurnStatus.CANCELLED and not outcome.publishes
        with pytest.raises(RuntimeError, match="never replayed"):
            await run.execute(work)
        assert calls == [1]

    asyncio.run(main())
