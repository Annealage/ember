"""Timeout recovery drives one live conversation, never replays a task."""

import asyncio
import json
from pathlib import Path

import pytest
from test_headless import finish, make_args, make_toolset, script_session

from annealage_ember import RecoveryInfo, TimeoutContext, headless
from annealage_ember.headless import run_prompt
from annealage_ember.session.base import AgentError, ToolResult, ToolUse

pytestmark = pytest.mark.asyncio


def recoverable(monkeypatch, script, *, interrupt=None):
    built = script_session(monkeypatch, script)
    build = headless._build_session

    def factory(*args, **kwargs):
        session = build(*args, **kwargs)

        async def stop():
            session.interrupted += 1
            if interrupt is not None:
                return await interrupt(session)
            await finish(session, text=("partial",), turn=len(session.submitted_turns))
            return True

        session.interrupt_for_timeout = stop
        return session

    monkeypatch.setattr("annealage_ember.headless._build_session", factory)
    return built


async def test_safe_diagnostic_and_continuation_keep_session_writes_and_budgets(monkeypatch, tmp_path):
    saved, observed = [], []

    async def script(session):
        turn = len(session.submitted_turns)
        if turn == 1:
            session.nonce = "retained-secret"
            session.emit(ToolUse(turn=1, tool_use_id="write", name="save", input={"v": "kept"}))
            await built.kwargs["server"].host_tool_table()["save"].handler({"v": "kept"})
            session.emit(ToolResult(tool_use_id="write", is_error=False, text="saved"))
        else:
            assert session.nonce == "retained-secret"
            await finish(session, text=("clear_path" if turn == 2 else "complete",), turn=turn)

    def on_timeout(context):
        observed.append(context)
        return "diagnose progress without restarting"

    def on_turn(result):
        return "continue original task" if result.text == "clear_path" else None

    built = recoverable(monkeypatch, script)
    result = await run_prompt("original", **make_args(
        tmp_path, tools=make_toolset(grant=("save",), saved=saved),
        work_timeout=0.01, interrupt_timeout=0.2, on_timeout=on_timeout,
        max_timeout_recoveries=1, max_followups=0, on_turn_result=on_turn,
    ))
    assert result.error is None and result.text == "complete"
    assert saved == ["kept"] and result.calls == (("save", True),)
    assert built.session.started == built.session.closed == 1
    assert [blocks[0]["text"] for blocks, _ in built.session.submitted_turns] == [
        "original", "diagnose progress without restarting", "continue original task",
    ]
    context, = observed
    assert isinstance(context, TimeoutContext) and isinstance(context.recovery, RecoveryInfo)
    assert (context.kind, context.interruption) == ("work_interval", "safe")
    assert context.result.calls == (("save", True),)
    assert context.elapsed > 0 and 0 < context.remaining < 5
    assert context.recovery.session_id == result.recovery.session_id


async def test_recovery_count_is_a_hard_bound(monkeypatch, tmp_path):
    async def script(session):
        pass

    contexts = []
    built = recoverable(monkeypatch, script)

    def timeout(context):
        contexts.append(context)
        return "diagnostic"

    result = await run_prompt("original", **make_args(
        tmp_path, work_timeout=0.01, interrupt_timeout=0.2,
        on_timeout=timeout, max_timeout_recoveries=2,
    ))
    assert result.timeout_kind == "work_interval" and result.error == "timeout"
    assert len(contexts) == 3 and len(built.session.submitted_turns) == 3
    assert built.session.started == 1


@pytest.mark.parametrize("mode", ("negative_ack", "no_boundary", "hung_abort", "missing_result"))
async def test_uncertain_interruption_never_submits_next_turn(monkeypatch, tmp_path, mode):
    async def script(session):
        if mode == "missing_result":
            session.emit(ToolUse(turn=1, tool_use_id="uncertain", name="save", input={}))

    async def interrupt(session):
        if mode == "hung_abort":
            await asyncio.Event().wait()
        if mode == "negative_ack":
            await finish(session)
            return False
        return True

    contexts = []
    built = recoverable(monkeypatch, script, interrupt=interrupt)
    result = await run_prompt("original", **make_args(
        tmp_path, state_dir=tmp_path / "conversation", work_timeout=0.01, interrupt_timeout=0.01,
        on_timeout=lambda context: contexts.append(context) or "must not submit",
        max_timeout_recoveries=2,
    ))
    assert result.error == "timeout" and result.timeout_kind == "work_interval"
    assert contexts[0].interruption == "unsafe"
    assert len(built.session.submitted_turns) == 1
    assert (tmp_path / "conversation" / "headless.json").is_file()
    if mode == "missing_result":
        assert built.session.interrupted == 0  # Never abort an unrecorded write.


async def test_unsupported_backend_reports_capability_without_retry(monkeypatch, tmp_path):
    async def script(session):
        pass

    contexts = []
    built = script_session(monkeypatch, script)
    result = await run_prompt("original", **make_args(
        tmp_path, backend="claude", work_timeout=0.01,
        on_timeout=lambda context: contexts.append(context) or "no",
        max_timeout_recoveries=2,
    ))
    assert result.error == "timeout"
    assert contexts[0].interruption == "unsupported"
    assert contexts[0].recovery.live_recovery_supported is False
    assert len(built.session.submitted_turns) == 1


async def test_hard_deadline_does_not_accept_observer_prompt(monkeypatch, tmp_path):
    async def script(session):
        pass

    contexts = []
    built = recoverable(monkeypatch, script)
    result = await run_prompt("original", **make_args(
        tmp_path, timeout=0.02, work_timeout=1,
        on_timeout=lambda context: contexts.append(context) or "never submit",
        max_timeout_recoveries=2,
    ))
    assert result.timeout_kind == "hard_deadline" and result.error == "timeout"
    assert contexts[0].kind == "hard_deadline" and contexts[0].remaining == 0
    assert contexts[0].interruption == "not_attempted"
    assert built.session.interrupted == 0 and len(built.session.submitted_turns) == 1


async def test_native_error_does_not_invoke_timeout_callback(monkeypatch, tmp_path):
    async def script(session):
        session.emit(AgentError(stderr="authentication failed", remediation=""))
        await finish(session, stop="error")

    contexts = []
    built = recoverable(monkeypatch, script)
    result = await run_prompt("original", **make_args(
        tmp_path, work_timeout=0.01, on_timeout=contexts.append, max_timeout_recoveries=2,
    ))
    assert "authentication failed" in result.error
    assert result.timeout_kind is None and contexts == [] and built.session.interrupted == 0


async def test_rpc_timeout_is_observational_not_a_generation_retry(monkeypatch, tmp_path):
    async def script(session):
        session.last_request_timeout = "prompt"
        session.emit(AgentError(stderr="RPC acknowledgement timed out", remediation=""))
        await finish(session, stop="rejected")

    contexts = []
    built = recoverable(monkeypatch, script)
    result = await run_prompt("original", **make_args(
        tmp_path, on_timeout=lambda context: contexts.append(context) or "never submit",
        max_timeout_recoveries=2,
    ))
    assert result.timeout_kind == "rpc_request_timeout"
    assert contexts[0].rpc_operation == "prompt"
    assert contexts[0].interruption == "not_attempted"
    assert len(built.session.submitted_turns) == 1


async def test_durable_explicit_resume_preserves_identity_and_reserves_turns(monkeypatch, tmp_path):
    state_dir = tmp_path / "conversation"

    async def script(session):
        kwargs = built.kwargs
        conversation = Path(kwargs["state_dir"]) / "omp" / "native.jsonl"
        conversation.parent.mkdir(exist_ok=True)
        conversation.write_text("native history", encoding="utf-8")
        kwargs["on_session_file"](str(conversation))
        await finish(session, turn=kwargs["resume_turn"] + 1)

    built = script_session(monkeypatch, script)
    first = await run_prompt("original", **make_args(tmp_path, state_dir=state_dir))
    first_kwargs = built.kwargs
    first_state = json.loads((state_dir / "headless.json").read_text())
    second = await run_prompt("new explicit input", **make_args(tmp_path, state_dir=state_dir, resume=True))
    second_state = json.loads((state_dir / "headless.json").read_text())
    assert first.recovery.session_id == second.recovery.session_id
    assert first_kwargs["session_id"] == built.kwargs["session_id"]
    assert first_kwargs["resume_turn"] == 0 and built.kwargs["resume_turn"] == 1
    assert first_state["turn"] == 1 and second_state["turn"] == 2
    assert built.kwargs["resume"] == first_state["conversation_file"]
    assert built.session.submitted_turns[0][0][0]["text"] == "new explicit input"
    assert Path(second.recovery.conversation_file).exists()
    assert (state_dir / "headless.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already contains"):
        await run_prompt("no implicit replacement", **make_args(tmp_path, state_dir=state_dir))
    Path(second.recovery.conversation_file).unlink()
    with pytest.raises(ValueError, match="missing"):
        await run_prompt("no cold fallback", **make_args(tmp_path, state_dir=state_dir, resume=True))


async def test_cancellation_retains_state_and_never_calls_timeout_policy(monkeypatch, tmp_path):
    state_dir = tmp_path / "conversation"
    entered = asyncio.Event()
    contexts = []

    async def script(session):
        entered.set()

    built = recoverable(monkeypatch, script)
    task = asyncio.create_task(run_prompt("original", **make_args(
        tmp_path, state_dir=state_dir, work_timeout=10, on_timeout=contexts.append,
    )))
    await entered.wait()
    with pytest.raises(ValueError, match="already in use"):
        await run_prompt("concurrent", **make_args(tmp_path, state_dir=state_dir))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert contexts == [] and built.session.closed == 1
    assert json.loads((state_dir / "headless.json").read_text())["turn"] == 1


async def test_callback_failure_never_retries(monkeypatch, tmp_path):
    async def script(session):
        pass

    def timeout(context):
        raise RuntimeError("policy failed")

    built = recoverable(monkeypatch, script)
    result = await run_prompt("original", **make_args(
        tmp_path, work_timeout=0.01, on_timeout=timeout, max_timeout_recoveries=2,
    ))
    assert "policy failed" in result.error and result.stop_reason == "error"
    assert len(built.session.submitted_turns) == 1


@pytest.mark.parametrize("extra", (
    {"work_timeout": 0}, {"work_timeout": float("inf")}, {"interrupt_timeout": False},
    {"max_timeout_recoveries": -1}, {"on_timeout": "not callable"},
    {"resume": True}, {"state_dir": ""}, {"state_dir": "state", "backend": "claude"},
    {"timeout": float("inf")},
))
async def test_invalid_recovery_configuration_fails_before_start(tmp_path, extra):
    with pytest.raises(ValueError):
        await run_prompt("original", **make_args(tmp_path, **extra))


async def test_write_result_is_in_native_history_before_abort(monkeypatch, tmp_path):
    release = asyncio.Event()
    wrote = asyncio.Event()
    result_recorded = asyncio.Event()
    workers, saved = [], []

    from claude_agent_sdk import tool
    from annealage_ember.headless import ToolSet
    from annealage_ember.tools import Grading, ok

    @tool("commit_value", "Write once", {})
    async def commit_value(args):
        await release.wait()
        saved.append("committed")
        wrote.set()
        return ok(text="committed")

    async def script(session):
        if len(session.submitted_turns) != 1:
            await finish(session, turn=2)
            return
        session.emit(ToolUse(turn=1, tool_use_id="write", name="commit_value", input={}))

        async def work():
            await built.kwargs["server"].host_tool_table()["commit_value"].handler({})
            session.emit(ToolResult(tool_use_id="write", is_error=False, text="committed"))
            result_recorded.set()

        workers.append(asyncio.create_task(work()))

    async def interrupt(session):
        assert wrote.is_set() and result_recorded.is_set()
        await finish(session)
        return True

    built = recoverable(monkeypatch, script, interrupt=interrupt)
    contexts = []
    # Release only once the SDK has actually paused tools for interval recovery,
    # not according to a racing sleep relative to the deadline.
    paused = asyncio.Event()
    original_settle = headless._Run.settle_tool_results

    async def settle(run):
        paused.set()
        await original_settle(run)

    monkeypatch.setattr(headless._Run, "settle_tool_results", settle)
    task = asyncio.create_task(run_prompt("original", **make_args(
        tmp_path, tools=ToolSet(
            tools=[commit_value], grading=Grading(read=(), view=(), write=("commit_value",)),
            grant=("commit_value",),
        ), work_timeout=0.01, interrupt_timeout=1, max_timeout_recoveries=1,
        on_timeout=lambda context: contexts.append(context) or "diagnose",
    )))
    await paused.wait()
    assert saved == [] and built.session.interrupted == 0
    refused = await built.kwargs["server"].host_tool_table()["commit_value"].handler({})
    assert refused["is_error"] is True
    release.set()
    result = await task
    await asyncio.gather(*workers)
    assert saved == ["committed"] and result.error is None
    assert result.calls == (("commit_value", True),)
    assert contexts[0].interruption == "safe"


async def test_cancel_during_diagnostic_policy_never_authorizes_next_input(monkeypatch, tmp_path):
    entered = asyncio.Event()

    async def script(session):
        pass

    async def timeout(context):
        entered.set()
        await asyncio.Event().wait()
        return "never sent"

    built = recoverable(monkeypatch, script)
    task = asyncio.create_task(run_prompt("original", **make_args(
        tmp_path, state_dir=tmp_path / "conversation", work_timeout=0.01,
        on_timeout=timeout, max_timeout_recoveries=1,
    )))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(built.session.submitted_turns) == 1
    assert built.session.closed == 1
    assert (tmp_path / "conversation" / "headless.json").is_file()


async def test_expired_callback_cannot_admit_turn_before_timer_is_serviced(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    real_time = loop.time
    advance = [0]
    contexts = []

    async def script(session):
        await finish(session)

    def on_turn(result):
        # Deterministically simulate a synchronous callback crossing the hard
        # deadline, without sleeping or depending on ready-queue/timer order.
        advance[0] += 10
        return "must never be sent"

    built = recoverable(monkeypatch, script)
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: real_time() + advance[0])
        result = await run_prompt("original", **make_args(
            tmp_path, timeout=1, on_turn_result=on_turn, max_followups=2,
            on_timeout=lambda context: contexts.append(context),
        ))
    assert result.timeout_kind == "hard_deadline" and result.error == "timeout"
    assert len(built.session.submitted_turns) == 1
    assert contexts[0].kind == "hard_deadline"


async def test_repeated_end_does_not_interrupt_awaited_handler_cleanup():
    from claude_agent_sdk import tool

    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []

    @tool("slow_write", "Exercise awaited teardown", {})
    async def slow_write(args):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.append("once")

    run = headless._Run("test__")
    worker = asyncio.create_task(run.guard(slow_write).handler({}))
    await entered.wait()
    run.end()
    await cleaning.wait()
    run.end()
    release.set()
    await run.drain()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert cleaned == ["once"]
