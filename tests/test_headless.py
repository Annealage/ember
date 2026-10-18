"""Tests for ``headless.run_prompt``: one private conversation, no human.

A scripted ``FakeSession`` (``session/fake.py``) replaces ``headless._build_session``,
so a test chooses exactly which events a turn produces and when: the text and
cost a result carries, a failure, a hang, a steered turn. That is where the
collection, the timeout and the cancellation are pinned.

The Claude backend is built for real and its ``ClaudeAgentOptions`` read,
without starting a CLI, since what matters there is what the model is *given*.
Nothing here needs the ``omp`` or ``claude`` binary, or ``omp_rpc``: the tests
that drive a real ``OmpSession`` over a fake omp client are in
``tests/test_headless_omp.py``, which CI skips where ``omp_rpc`` is not installed.
"""

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage, tool

import annealage_ember
from annealage_ember import headless
from annealage_ember.headless import RunResult, ToolSet, run_prompt
from annealage_ember.hosted_state import usage_event
from annealage_ember.session.base import (
    AGENT_READY,
    AGENT_UNAVAILABLE,
    AgentError,
    AgentRuntimeResolved,
    AgentStatus,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnEnd,
)
from annealage_ember.session.fake import FakeSession
from annealage_ember.session.permissions import HeadlessBroker
from annealage_ember.tools import Grading, asks_the_human, ok

pytestmark = pytest.mark.asyncio

TOKENS = {"input": 11, "output": 7, "cache_read": 0, "cache_write": 0}


def make_toolset(*, grant=(), saved=None):
    """A read tool, a view tool and a write tool; ``saved`` records the write."""
    saved = saved if saved is not None else []

    @tool("lookup", "look something up", {"q": str})
    async def lookup(args):
        return ok(text="found %s" % args.get("q", ""))

    @tool("pan", "move the view", {})
    async def pan(args):
        return ok(text="panned")

    @tool("save", "save a value", {"v": str})
    async def save(args):
        saved.append(args["v"])
        return ok(text="saved")

    return ToolSet(
        tools=[lookup, pan, save],
        grading=Grading(read=("lookup",), view=("pan",), write=("save",)),
        grant=grant,
    )


def make_args(tmp_path, **overrides):
    args = {
        "tools": make_toolset(),
        "backend": "omp",
        "model": "prov/model",
        "cwd": tmp_path,
        "timeout": 5,
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# A scripted session in place of the backend
# ---------------------------------------------------------------------------


class Scripted(FakeSession):
    """A ``FakeSession`` whose turn runs ``script(session)``."""

    def __init__(self, on_event, script, **kwargs):
        super().__init__(on_event, **kwargs)
        self.script = script

    async def submit_turn(self, blocks, viewer=None):
        await super().submit_turn(blocks, viewer)
        await self.script(self)


def script_session(monkeypatch, script, *, on_start=None):
    """Make ``run_prompt`` drive ``Scripted(script)``; returns what it was built
    from (``.session``, ``.kwargs``)."""
    built = SimpleNamespace(session=None, kwargs=None)

    def build(backend, on_event, **kwargs):
        session = Scripted(on_event, script)
        if on_start is not None:
            real_start = session.start

            async def start():
                await real_start()
                on_start(session)

            session.start = start
        built.session = session
        built.kwargs = dict(kwargs, backend=backend)
        return session

    monkeypatch.setattr(headless, "_build_session", build)
    return built


async def finish(session, *, text=("done",), stop="end_turn", cost=0.25, tokens=TOKENS, turn=1):
    for piece in text:
        session.emit(TextDelta(turn=turn, text=piece))
    session.emit(TurnEnd(turn=turn, stop_reason=stop, cost_usd=cost, tokens=tokens))


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


async def test_a_turn_returns_its_text_cost_and_tokens(monkeypatch, tmp_path):
    async def script(session):
        await finish(session, text=("Hello, ", "world."))

    built = script_session(monkeypatch, script)
    result = await run_prompt("say hello", system="Be brief.", **make_args(tmp_path))

    assert result == RunResult(
        text="Hello, world.",
        stop_reason="end_turn",
        cost_usd=0.25,
        tokens=TOKENS,
        error=None,
        calls=(),
    )
    # One user message, as one text block, and the system text goes in as the
    # session's instructions.
    assert built.session.submitted_turns == [([{"type": "text", "text": "say hello"}], None)]
    assert built.kwargs["instructions"] == "Be brief."
    assert built.kwargs["model"] == "prov/model"
    assert Path(built.kwargs["cwd"]) == tmp_path
    assert built.session.started == 1 and built.session.closed == 1


async def test_runtime_callback_receives_only_backend_reported_identity(monkeypatch, tmp_path):
    observed = []

    async def script(session):
        await finish(session)

    def on_start(session):
        session.emit(
            AgentRuntimeResolved(
                backend="omp",
                provider="observed-provider",
                model="observed-model",
                effort="high",
                provenance="get_state",
            )
        )

    script_session(monkeypatch, script, on_start=on_start)
    await run_prompt(
        "go",
        on_runtime=observed.append,
        **make_args(tmp_path, model="requested-provider/requested-model"),
    )

    assert observed == [
        AgentRuntimeResolved(
            backend="omp",
            provider="observed-provider",
            model="observed-model",
            effort="high",
            provenance="get_state",
        )
    ]


async def test_a_missing_turn_cost_stays_unknown(monkeypatch, tmp_path):
    async def script(session):
        await finish(session, cost=None, tokens=None)

    script_session(monkeypatch, script)
    result = await run_prompt("say hello", **make_args(tmp_path))

    assert result.cost_usd is None
    assert result.tokens is None


async def test_tool_calls_are_recorded_by_name_and_outcome_only(monkeypatch, tmp_path):
    async def script(session):
        # Claude names a tool mcp__<server>__<tool>, omp by its own name; both
        # come back as the tool set's name.
        session.emit(
            ToolUse(turn=1, tool_use_id="a", name="mcp__toy__lookup", input={"q": "s3cret"})
        )
        session.emit(ToolResult(tool_use_id="a", is_error=False, text="found s3cret"))
        session.emit(ToolUse(turn=1, tool_use_id="b", name="save", input={"v": "s3cret"}))
        session.emit(ToolResult(tool_use_id="b", is_error=True, text="refused"))
        session.emit(ToolUse(turn=1, tool_use_id="c", name="pan", input={}))
        await finish(session)

    script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path))

    # The call that never returned counts as not ok.
    assert result.calls == (("lookup", True), ("save", False), ("pan", False))
    assert "s3cret" not in repr(result)


async def test_a_steered_turn_keeps_waiting_for_the_real_end(monkeypatch, tmp_path):
    async def script(session):
        session.emit(TextDelta(turn=1, text="first "))
        session.emit(TurnEnd(turn=1, stop_reason="steered", cost_usd=0.0))
        session.emit(TextDelta(turn=2, text="second"))
        await asyncio.sleep(0.05)
        session.emit(TurnEnd(turn=2, stop_reason="end_turn", cost_usd=0.5, tokens=TOKENS))

    script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path))

    assert (result.text, result.stop_reason, result.cost_usd) == ("first second", "end_turn", 0.5)
    assert result.error is None


# ---------------------------------------------------------------------------
# Failure comes back as a value
# ---------------------------------------------------------------------------


async def test_a_backend_error_is_returned_not_raised(monkeypatch, tmp_path):
    async def script(session):
        session.emit(TextDelta(turn=1, text="partial"))
        session.emit(AgentError(stderr="the model fell over", remediation="try later"))
        session.emit(TurnEnd(turn=1, stop_reason="error", cost_usd=0.0))

    built = script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.error == "the model fell over"
    assert (result.text, result.stop_reason) == ("partial", "error")
    assert built.session.closed == 1


async def test_a_session_that_will_not_start_is_an_error_result(monkeypatch, tmp_path):
    async def script(session):
        raise AssertionError("no turn is sent to a session that did not start")

    def on_start(session):
        session.set_status(AGENT_UNAVAILABLE)
        session.emit(AgentError(stderr="omp: not logged in", remediation="log in"))

    built = script_session(monkeypatch, script, on_start=on_start)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.error == "omp: not logged in"
    assert result.stop_reason == "error" and result.text == ""
    assert built.session.submitted_turns == []
    assert built.session.closed == 1


async def test_a_session_that_dies_mid_turn_ends_the_run(monkeypatch, tmp_path):
    async def script(session):
        session.emit(TextDelta(turn=1, text="half an answ"))
        # SdkSession._fail's order: the status change, then the error.
        session.set_status(AGENT_UNAVAILABLE)
        session.emit(AgentStatus(status=AGENT_UNAVAILABLE))
        session.emit(AgentError(stderr="the claude process exited", remediation=""))
        await asyncio.sleep(3600)

    script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.error == "the claude process exited"
    assert (result.text, result.stop_reason) == ("half an answ", "error")


async def test_an_exception_from_the_session_is_an_error_result(monkeypatch, tmp_path):
    async def script(session):
        raise OSError("pipe closed")

    built = script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.error == "OSError: pipe closed"
    assert result.stop_reason == "error"
    assert built.session.closed == 1


async def test_a_timeout_closes_the_session_and_keeps_the_partial_text(monkeypatch, tmp_path):
    async def script(session):
        session.emit(TextDelta(turn=1, text="so far"))
        await asyncio.sleep(3600)

    built = script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path, timeout=0.1))

    assert (result.stop_reason, result.error, result.text) == ("timeout", "timeout", "so far")
    assert built.session.closed == 1


async def test_cancelling_closes_the_session_and_reraises(monkeypatch, tmp_path):
    reached = asyncio.Event()

    async def script(session):
        reached.set()
        await asyncio.sleep(3600)

    built = script_session(monkeypatch, script)
    task = asyncio.ensure_future(run_prompt("go", **make_args(tmp_path, timeout=60)))
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert built.session.closed == 1


async def test_the_state_directory_exists_during_the_run_and_is_gone_after(monkeypatch, tmp_path):
    seen = {}

    async def script(session):
        state = Path(built.kwargs["state_dir"])
        seen["during"] = state.is_dir()
        seen["inside_cwd"] = state == tmp_path or tmp_path in state.parents
        (state / "scratch").write_text("x")
        await finish(session)

    built = script_session(monkeypatch, script)
    await run_prompt("go", **make_args(tmp_path))

    assert seen == {"during": True, "inside_cwd": False}
    assert not Path(built.kwargs["state_dir"]).exists()
    # The caller's scratch directory is left exactly as it was.
    assert list(tmp_path.iterdir()) == []


async def test_the_state_directory_is_removed_after_a_timeout(monkeypatch, tmp_path):
    async def script(session):
        await asyncio.sleep(3600)

    built = script_session(monkeypatch, script)
    await run_prompt("go", **make_args(tmp_path, timeout=0.1))

    assert not Path(built.kwargs["state_dir"]).exists()


# ---------------------------------------------------------------------------
# The Claude backend: what the model is given
# ---------------------------------------------------------------------------


async def test_the_claude_session_has_no_builtin_tools_and_no_settings(monkeypatch, tmp_path):
    real = headless._build_session
    claude = {}

    def build(backend, on_event, **kwargs):
        claude["session"] = real(backend, on_event, **kwargs)
        return Scripted(on_event, script)

    async def script(session):
        await finish(session)

    monkeypatch.setattr(headless, "_build_session", build)
    result = await run_prompt(
        "go", **make_args(tmp_path, backend="claude", tools=make_toolset(grant=("save",)))
    )
    assert result.error is None

    session = claude["session"]
    options = session._build_options()
    assert options.tools == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.model == "prov/model"
    assert Path(options.cwd) == tmp_path
    # Read, view and the one grant; the ungranted write tool is absent.
    assert options.allowed_tools == ["mcp__toy__lookup", "mcp__toy__pan", "mcp__toy__save"]
    assert list(options.mcp_servers) == ["toy"]
    assert options.sandbox is None
    # No transcript of the run is left under ~/.claude/projects.
    assert options.extra_args == {"no-session-persistence": None}
    assert session.sandbox_status().requested is False


async def test_thinking_is_passed_as_claude_effort(monkeypatch, tmp_path):
    real = headless._build_session
    sessions = {}

    def build(backend, on_event, **kwargs):
        sessions["session"] = real(backend, on_event, **kwargs)
        return Scripted(on_event, script)

    async def script(session):
        await finish(session)

    monkeypatch.setattr(headless, "_build_session", build)
    result = await run_prompt("go", **make_args(tmp_path, backend="claude", thinking="medium"))
    assert result.error is None
    assert sessions["session"]._build_options().effort == "medium"


async def test_the_claude_broker_decides_a_builtin_a_grant_and_an_ungranted_write(
    monkeypatch, tmp_path
):
    real = headless._build_session
    claude = {}
    decisions = {}

    def build(backend, on_event, **kwargs):
        claude["session"] = real(backend, on_event, **kwargs)
        return Scripted(on_event, script)

    async def script(session):
        # While the run is live, as the CLI would ask.
        ask = claude["session"]._can_use_tool
        decisions["bash"] = await ask("Bash", {"command": "ls"}, None)
        decisions["save"] = await ask("mcp__toy__save", {"v": "x"}, None)
        decisions["add_note"] = await ask("mcp__toy__add_note", {"text": "x"}, None)
        await finish(session)

    monkeypatch.setattr(headless, "_build_session", build)
    tools = make_toolset(grant=("save",))
    await run_prompt("go", **make_args(tmp_path, backend="claude", tools=tools))

    for name in ("bash", "add_note"):
        assert isinstance(decisions[name], PermissionResultDeny)
    # Allowed, and not as a session-wide rule the CLI would keep applying.
    assert isinstance(decisions["save"], PermissionResultAllow)
    assert not decisions["save"].updated_permissions

    # Once the run is over, even the grant is refused.
    late = await claude["session"]._can_use_tool("mcp__toy__save", {"v": "x"}, None)
    assert isinstance(late, PermissionResultDeny)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


async def test_codex_is_refused_with_a_reason(tmp_path):
    with pytest.raises(ValueError, match="codex.*/mcp"):
        await run_prompt("go", **make_args(tmp_path, backend="codex"))


@pytest.mark.parametrize(
    "request_timeout",
    [0, -1, True, float("nan"), float("inf"), 10**400, "180"],
)
async def test_invalid_omp_request_timeout_is_rejected(tmp_path, request_timeout):
    with pytest.raises(ValueError, match="omp_request_timeout must be a finite number"):
        await run_prompt("go", **make_args(tmp_path, omp_request_timeout=request_timeout))


async def test_a_usage_sink_is_refused_for_claude_and_when_not_callable(tmp_path):
    with pytest.raises(ValueError, match="usage_sink is only supported with backend='omp'"):
        await run_prompt(
            "go", **make_args(tmp_path, backend="claude", usage_sink=lambda event: None)
        )
    with pytest.raises(ValueError, match="usage_sink must be a callable"):
        await run_prompt("go", **make_args(tmp_path, usage_sink="not a function"))


@pytest.mark.parametrize(
    "builtin_tools",
    [("bash",), ("web_search", "write"), ("fetch",), ("web_search", "web_search"), "web_search", ["browser"]],
)
async def test_builtin_tools_outside_the_network_read_allowlist_are_refused(
    tmp_path, builtin_tools
):
    with pytest.raises(ValueError, match="builtin_tools"):
        await run_prompt("go", **make_args(tmp_path, builtin_tools=builtin_tools))


async def test_builtin_tools_are_refused_for_claude(tmp_path):
    with pytest.raises(ValueError, match="builtin_tools is only supported with backend='omp'"):
        await run_prompt(
            "go", **make_args(tmp_path, backend="claude", builtin_tools=("web_search",))
        )


async def test_omp_request_timeout_is_rejected_for_claude(tmp_path):
    with pytest.raises(ValueError, match="only supported with backend='omp'"):
        await run_prompt("go", **make_args(tmp_path, backend="claude", omp_request_timeout=180))


@pytest.mark.parametrize(
    "options, message, backend",
    [
        ({"provider_session_id": "affinity"}, "provider_session_id is only supported", "claude"),
        ({"cache_retention": "short"}, "cache_retention is only supported", "claude"),
        (
            {"provider_session_id": ""},
            "provider_session_id must be None or a non-empty string",
            "omp",
        ),
        (
            {"provider_session_id": 42},
            "provider_session_id must be None or a non-empty string",
            "omp",
        ),
        ({"cache_retention": "invalid"}, "cache_retention must be None", "omp"),
    ],
)
async def test_omp_affinity_options_are_validated(options, message, backend, tmp_path):
    with pytest.raises(ValueError, match=message):
        await run_prompt("go", **make_args(tmp_path, backend=backend, **options))


@pytest.mark.parametrize("cache_retention", ["short", "long", "none"])
async def test_cache_retention_accepts_documented_values(cache_retention, tmp_path):
    headless._check_arguments(
        "go",
        make_toolset(),
        "omp",
        "prov/model",
        tmp_path,
        5,
        None,
        None,
        cache_retention=cache_retention,
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"backend": "gpt"}, "backend must be one of omp, claude"),
        ({"model": ""}, "model must be None or a non-empty string"),
        ({"model": 3}, "model must be None or a non-empty string"),
        ({"timeout": 0}, "timeout must be"),
        ({"timeout": -3}, "timeout must be"),
        ({"timeout": None}, "timeout must be"),
        ({"tools": ["lookup"]}, "tools must be a ToolSet"),
    ],
)
async def test_bad_arguments_raise_value_error(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        await run_prompt("go", **make_args(tmp_path, **overrides))


@pytest.mark.parametrize("thinking", ["", "auto", "HIGH", 1])
async def test_invalid_thinking_level_is_rejected(tmp_path, thinking):
    with pytest.raises(ValueError, match="thinking must be"):
        await run_prompt("go", **make_args(tmp_path, thinking=thinking))


async def test_claude_rejects_omp_only_xhigh(tmp_path):
    with pytest.raises(ValueError, match="thinking must be.*for claude"):
        await run_prompt("go", **make_args(tmp_path, backend="claude", thinking="xhigh"))


async def test_a_missing_cwd_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="cwd must be an existing directory"):
        await run_prompt("go", **make_args(tmp_path, cwd=tmp_path / "nowhere"))


async def test_an_empty_prompt_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="prompt must be"):
        await run_prompt("  ", **make_args(tmp_path))


async def test_a_grant_must_name_a_write_tool_in_the_set():
    with pytest.raises(ValueError, match="'nothing'.*not a tool in this set"):
        make_toolset(grant=("nothing",))
    with pytest.raises(ValueError, match="'lookup'.*read or view"):
        make_toolset(grant=("lookup",))
    with pytest.raises(ValueError, match="'pan'.*read or view"):
        make_toolset(grant=("pan",))


async def test_a_lone_string_grant_is_one_name_not_its_letters():
    assert make_toolset(grant="save").grant == ("save",)


async def test_a_tool_set_is_checked_as_the_tool_server_checks_it():
    @tool("extra", "ungraded", {})
    async def extra(args):
        return ok(text="x")

    with pytest.raises(ValueError, match="extra.*not classified"):
        ToolSet(tools=[extra], grading=Grading(read=(), view=(), write=()))
    with pytest.raises(ValueError, match="gone.*not built"):
        ToolSet(tools=[extra], grading=Grading(read=("extra", "gone"), view=(), write=()))
    with pytest.raises(ValueError, match="both read and write"):
        ToolSet(tools=[extra], grading=Grading(read=("extra",), view=(), write=("extra",)))


async def test_the_public_names_import_from_the_package():
    assert annealage_ember.run_prompt is run_prompt
    assert annealage_ember.RunResult is RunResult
    assert annealage_ember.ToolSet is ToolSet
    assert not hasattr(annealage_ember, "not_a_thing")


async def test_importing_the_package_does_not_import_the_backends():
    code = (
        "import sys, annealage_ember;"
        "assert 'omp_rpc' not in sys.modules and 'claude_agent_sdk' not in sys.modules"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, env=env)
    assert await process.wait() == 0


# ---------------------------------------------------------------------------
# What is returned, and what is refused, once a run is over
# ---------------------------------------------------------------------------


async def test_text_is_what_the_model_said_after_its_last_tool_result(monkeypatch, tmp_path):
    async def script(session):
        session.emit(TextDelta(turn=1, text="Let me look. "))
        session.emit(ToolUse(turn=1, tool_use_id="a", name="lookup", input={}))
        session.emit(ToolResult(tool_use_id="a", is_error=False, text="found"))
        session.emit(TextDelta(turn=1, text="Now saving. "))
        session.emit(ToolUse(turn=1, tool_use_id="b", name="pan", input={}))
        session.emit(ToolResult(tool_use_id="b", is_error=False, text="panned"))
        await finish(session, text=("The ", "answer."))

    script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.text == "The answer."


async def test_a_backend_that_cannot_be_built_is_an_error_result(monkeypatch, tmp_path):
    # omp_rpc is installed apart from this package: its absence is an import error.
    monkeypatch.setitem(sys.modules, "annealage_ember.session.omp", None)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.stop_reason == "error"
    assert result.error is not None and "ModuleNotFoundError" in result.error
    assert result.text == "" and result.calls == ()


async def test_a_tool_that_asks_the_human_is_refused_in_a_tool_set():
    @asks_the_human
    @tool("resolve", "resolve a comment", {})
    async def resolve(args):
        return ok(text="resolved")

    with pytest.raises(ValueError, match="resolve.*ask the product owner"):
        ToolSet(tools=[resolve], grading=Grading(read=(), view=(), write=("resolve",)))


async def test_the_headless_broker_denies_a_granted_call_once_shut_down():
    broker = HeadlessBroker(lambda event: None, granted=("save",))

    assert (await broker.ask("save", {}, None)).allow is True
    assert (await broker.ask("other", {}, None)).allow is False

    broker.shutdown()
    refused = await broker.ask("save", {}, None)
    assert refused.allow is False and "shutting down" in refused.message


async def test_a_session_that_will_not_close_does_not_hold_the_result_up(monkeypatch, tmp_path):
    monkeypatch.setattr(headless, "_CLOSE_TIMEOUT", 0.1)
    order = []

    async def script(session):
        await asyncio.sleep(3600)

    async def kill():
        order.append("kill")

    async def close():
        order.append("close")
        await asyncio.sleep(3600)

    def on_start(session):
        session.kill = kill
        session.close = close

    script_session(monkeypatch, script, on_start=on_start)
    began = time.monotonic()
    result = await run_prompt("go", **make_args(tmp_path, timeout=0.1))

    assert result.error == "timeout"
    assert order == ["kill", "close"]
    assert time.monotonic() - began < 3


async def test_a_run_that_finished_cleanly_does_not_kill_the_session(monkeypatch, tmp_path):
    killed = []

    async def script(session):
        await finish(session)

    def on_start(session):
        async def kill():
            killed.append(True)

        session.kill = kill

    script_session(monkeypatch, script, on_start=on_start)
    result = await run_prompt("go", **make_args(tmp_path))

    assert result.error is None
    assert killed == []


async def test_ending_a_run_twice_is_harmless():
    run = headless._Run("mcp__toy__")
    run.end()
    run.end()
    await run.drain()
    assert run.over


async def test_a_claude_result_that_failed_is_an_error_not_a_success(monkeypatch, tmp_path):
    real = headless._build_session

    def build(backend, on_event, **kwargs):
        session = real(backend, on_event, **kwargs)

        async def start():
            pass

        async def submit_turn(blocks, viewer=None):
            session._turn = 1
            session._handle(TextMessageStub())
            session._handle(
                ResultMessage(
                    subtype="success",
                    duration_ms=10,
                    duration_api_ms=5,
                    is_error=True,
                    num_turns=1,
                    session_id="s",
                    stop_reason="end_turn",
                    total_cost_usd=0.001,
                    result="Credit balance is too low",
                    errors=None,
                    api_error_status=402,
                )
            )

        session.start = start
        session.agent_status = lambda: AGENT_READY
        session.submit_turn = submit_turn
        return session

    class TextMessageStub:
        """Not a message the session handles: the failed result is the point."""

    monkeypatch.setattr(headless, "_build_session", build)
    result = await run_prompt("go", **make_args(tmp_path, backend="claude"))

    assert result.stop_reason == "error"
    assert result.error == "Credit balance is too low (API status 402)"
    assert result.cost_usd == pytest.approx(0.001)


@pytest.mark.parametrize("cwd", [None, 3, 3.5, b"", ["x"], object()])
async def test_a_cwd_that_is_not_a_path_raises_value_error(tmp_path, cwd):
    with pytest.raises(ValueError, match="cwd must be an existing directory"):
        await run_prompt("go", **make_args(tmp_path, cwd=cwd))


async def test_a_cwd_that_is_a_file_raises_value_error(tmp_path):
    a_file = tmp_path / "f"
    a_file.write_text("x")
    with pytest.raises(ValueError, match="cwd must be an existing directory"):
        await run_prompt("go", **make_args(tmp_path, cwd=a_file))


async def test_a_str_cwd_is_accepted(monkeypatch, tmp_path):
    async def script(session):
        await finish(session)

    script_session(monkeypatch, script)
    result = await run_prompt("go", **make_args(tmp_path, cwd=str(tmp_path)))
    assert result.error is None


@pytest.mark.parametrize(
    ("backend", "model", "model_role", "message"),
    [
        ("omp", None, "fast", "model_role must be one of"),
        ("omp", "prov/model", "smol", "model and model_role are mutually exclusive"),
        ("claude", None, "smol", "supported only with backend='omp'"),
    ],
)
async def test_model_role_rejects_unsupported_requests(
    tmp_path, backend, model, model_role, message
):
    with pytest.raises(ValueError, match=message):
        await run_prompt(
            "go",
            **make_args(tmp_path, backend=backend, model=model, model_role=model_role),
        )


# ---------------------------------------------------------------------------
# Bounded follow-ups retain the identical live conversation and tool state
# ---------------------------------------------------------------------------


async def test_followup_uses_same_live_session_and_never_replays_successful_tools(
    monkeypatch, tmp_path
):
    saved, observed, sessions = [], [], []
    tools = make_toolset(grant=("save",), saved=saved)

    async def script(session):
        sessions.append(session)
        turn = len(session.submitted_turns)
        if turn == 1:
            session.emit(ToolUse(turn=turn, tool_use_id="saved", name="save", input={"v": "kept"}))
            await built.kwargs["server"].host_tool_table()["save"].handler({"v": "kept"})
            session.emit(ToolResult(tool_use_id="saved", is_error=False, text="saved"))
            await finish(session, text=("forgot the submission",), turn=turn)
        else:
            assert saved == ["kept"]
            # A duplicate old terminal event must not terminate the follow-up.
            session.emit(TurnEnd(turn=1, stop_reason="end_turn", cost_usd=999))
            session.emit(ToolUse(turn=turn, tool_use_id="lookup", name="lookup", input={}))
            session.emit(ToolResult(tool_use_id="lookup", is_error=False, text="found"))
            await finish(session, text=("submitted",), cost=0.5, turn=turn)

    def on_turn(result):
        observed.append(result)
        return "Submit the missing result; keep the existing saved value." if len(observed) == 1 else None

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "review", **make_args(tmp_path, tools=tools, on_turn_result=on_turn, max_followups=2)
    )

    assert sessions == [built.session, built.session]
    assert built.session.submitted_turns == [
        ([{"type": "text", "text": "review"}], None),
        ([{"type": "text", "text": "Submit the missing result; keep the existing saved value."}], None),
    ]
    assert built.session.started == 1 and built.session.closed == 1
    assert built.session.set_model_calls == []
    assert saved == ["kept"]
    assert [turn.calls for turn in observed] == [(("save", True),), (("lookup", True),)]
    assert [turn.cost_usd for turn in observed] == [0.25, 0.5]
    assert result.calls == (("save", True), ("lookup", True))
    assert result.cost_usd == 0.75
    assert result.tokens == {key: 2 * value for key, value in TOKENS.items()}
    assert (result.text, result.error) == ("submitted", None)


@pytest.mark.parametrize("max_followups", [0, 1, 2])
async def test_followup_bound_is_hard_even_if_callback_always_requests_more(
    monkeypatch, tmp_path, max_followups
):
    observed = []

    async def script(session):
        await finish(session, turn=len(session.submitted_turns), stop="max_tokens")

    async def on_turn(result):
        await asyncio.sleep(0)
        observed.append(result)
        return "continue"

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=on_turn, max_followups=max_followups)
    )
    assert len(built.session.submitted_turns) == max_followups + 1
    assert len(observed) == max_followups + 1
    assert result.error is None


@pytest.mark.parametrize(
    "stop", ["rejected", "error", "backend_exited", "not_sent", "cancelled",
             "error_max_turns", "max_turns", "error_max_budget_usd", "max_budget_usd"]
)
async def test_rejections_failures_and_native_limits_never_invoke_callback(
    monkeypatch, tmp_path, stop
):
    observed = []

    async def script(session):
        await finish(session, stop=stop)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=lambda r: observed.append(r) or "retry",
                          max_followups=2)
    )
    assert observed == []
    assert len(built.session.submitted_turns) == 1
    assert result.stop_reason == stop


@pytest.mark.parametrize("kind", ["agent_error", "sdk_result_error", "exception"])
async def test_infrastructure_errors_never_invoke_callback(monkeypatch, tmp_path, kind):
    observed = []

    async def script(session):
        if kind == "exception":
            raise OSError("provider disconnected")
        if kind == "sdk_result_error":
            session.last_result_error = "native turn limit reached"
        else:
            session.emit(AgentError(stderr="provider disconnected"))
        await finish(session)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=observed.append, max_followups=2)
    )
    assert observed == []
    assert len(built.session.submitted_turns) == 1
    assert result.error


async def test_tool_errors_are_exact_per_turn_and_aggregate_without_success_contents(
    monkeypatch, tmp_path
):
    observed = []
    failures = ["schema: missing required field 's3cret'\n  at $.value", "authority: write denied"]

    async def script(session):
        turn = len(session.submitted_turns)
        # Tool IDs may be reused by the backend in a new turn.
        session.emit(ToolUse(turn=turn, tool_use_id="same", name="mcp__toy__save", input={}))
        session.emit(ToolResult(tool_use_id="same", is_error=True, text=failures[turn - 1]))
        session.emit(ToolUse(turn=turn, tool_use_id="ok", name="lookup", input={}))
        session.emit(ToolResult(tool_use_id="ok", is_error=False, text="successful secret"))
        await finish(session, turn=turn, stop="ended_by_tool")

    def on_turn(result):
        observed.append(result)
        return "Correct the exact error" if len(observed) == 1 else None

    script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=on_turn, max_followups=1)
    )
    assert [r.tool_errors for r in observed] == [(("save", failures[0]),), (("save", failures[1]),)]
    assert result.tool_errors == tuple(("save", text) for text in failures)
    assert result.calls == (("save", False), ("lookup", True)) * 2
    assert "s3cret" not in repr(result) and "successful secret" not in repr(result)


@pytest.mark.parametrize("unknown_first", [True, False])
async def test_aggregate_usage_preserves_unknown_dimensions(monkeypatch, tmp_path, unknown_first):
    async def script(session):
        turn = len(session.submitted_turns)
        unknown = (turn == 1) == unknown_first
        await finish(
            session, turn=turn, cost=None if unknown else 0.25,
            tokens={"input": None, "output": 3} if unknown else {"input": 2, "output": 7, "reasoning": 1},
        )

    script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=lambda _: "continue", max_followups=1)
    )
    assert result.cost_usd is None
    assert result.tokens == {"input": None, "output": 10, "reasoning": None}


async def test_request_usage_keeps_one_owner_and_positional_ids_across_followups(
    monkeypatch, tmp_path
):
    events = []

    async def script(session):
        turn = len(session.submitted_turns)
        built.kwargs["on_request_usage"](usage_event(
            session_id=built.kwargs["session_id"], conversation_id="same-conversation",
            turn_index=turn, request_n=1, backend="omp", model="model", provider="provider",
            provider_request_id="repeated-response-id", tokens=TOKENS, cost_usd=0.25,
        ))
        await finish(session, turn=turn)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=lambda _: "continue", max_followups=1,
                          usage_sink=events.append)
    )
    assert len(events) == 2
    assert len({e["session_id"] for e in events}) == 1
    assert len({e["event_id"] for e in events}) == 2
    assert sorted((e["turn_index"], e["request_n"]) for e in events) == [(1, 1), (2, 1)]
    # TurnEnd totals are summed once, not added again from per-request events.
    assert result.cost_usd == 0.5


@pytest.mark.parametrize("wait_in", ["callback", "followup"])
async def test_whole_run_timeout_includes_callback_and_followup(monkeypatch, tmp_path, wait_in):
    cleaned = []

    async def script(session):
        turn = len(session.submitted_turns)
        if turn == 2:
            session.emit(TextDelta(turn=turn, text="partial correction"))
            try:
                await asyncio.sleep(3600)
            finally:
                cleaned.append("followup")
        else:
            await finish(session)

    async def on_turn(_):
        if wait_in == "callback":
            try:
                await asyncio.sleep(3600)
            finally:
                cleaned.append("callback")
        return "correct"

    built = script_session(monkeypatch, script)
    result = await asyncio.wait_for(run_prompt(
        "go", **make_args(tmp_path, timeout=0.05, on_turn_result=on_turn, max_followups=2)
    ), 2)
    assert (result.stop_reason, result.error) == ("timeout", "timeout")
    assert cleaned == [wait_in]
    assert built.session.closed == 1
    if wait_in == "followup":
        assert result.text == "partial correction"
        assert result.cost_usd is None
        assert result.tokens == {key: None for key in TOKENS}


@pytest.mark.parametrize("wait_in", ["callback", "followup"])
async def test_cancellation_during_continuation_cleans_up_and_reraises(
    monkeypatch, tmp_path, wait_in
):
    reached = asyncio.Event()
    cleaned = []

    async def wait_here():
        reached.set()
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.append(wait_in)

    async def script(session):
        if len(session.submitted_turns) == 2:
            await wait_here()
        else:
            await finish(session)

    async def on_turn(_):
        if wait_in == "callback":
            await wait_here()
        return "correct"

    built = script_session(monkeypatch, script)
    task = asyncio.create_task(run_prompt(
        "go", **make_args(tmp_path, on_turn_result=on_turn, max_followups=2)
    ))
    await asyncio.wait_for(reached.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert cleaned == [wait_in]
    assert built.session.closed == 1


@pytest.mark.parametrize("value", [-1, True, 1.5, None, "2"])
async def test_invalid_followup_bounds_are_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="max_followups must be a nonnegative integer"):
        await run_prompt("go", **make_args(tmp_path, max_followups=value))


async def test_noncallable_turn_callback_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="on_turn_result must be a callable"):
        await run_prompt("go", **make_args(tmp_path, on_turn_result="retry"))


@pytest.mark.parametrize("value", ["", "  ", 1])
async def test_invalid_callback_result_stops_without_retry(monkeypatch, tmp_path, value):
    async def script(session):
        await finish(session)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=lambda _: value, max_followups=2)
    )
    assert result.stop_reason == "error"
    assert "on_turn_result must return None or a non-empty string" in result.error
    assert len(built.session.submitted_turns) == 1


async def test_inflight_tools_settle_before_callback_and_guard_remains_live(
    monkeypatch, tmp_path
):
    saved, observed, workers = [], [], []

    @tool("save", "save asynchronously", {"v": str})
    async def save(args):
        await asyncio.sleep(0.01)
        saved.append(args["v"])
        return ok(text="saved")

    async def script(session):
        turn = len(session.submitted_turns)
        call_id = str(turn)
        session.emit(ToolUse(turn=turn, tool_use_id=call_id, name="save", input={}))

        async def work():
            await built.kwargs["server"].host_tool_table()["save"].handler({"v": call_id})
            session.emit(ToolResult(tool_use_id=call_id, is_error=False, text="saved"))

        workers.append(asyncio.create_task(work()))
        await asyncio.sleep(0)  # Enter the guarded handler before TurnEnd.
        await finish(session, turn=turn)

    def on_turn(result):
        observed.append(result)
        assert saved == [str(n) for n in range(1, len(observed) + 1)]
        assert result.calls == (("save", True),)
        return "finish" if len(observed) == 1 else None

    built = script_session(monkeypatch, script)
    tools = ToolSet(tools=[save], grading=Grading(read=(), view=(), write=("save",)), grant=("save",))
    result = await run_prompt(
        "go", **make_args(tmp_path, tools=tools, on_turn_result=on_turn, max_followups=1)
    )
    await asyncio.gather(*workers)
    late = await built.kwargs["server"].host_tool_table()["save"].handler({"v": "late"})
    assert late["is_error"] is True
    assert saved == ["1", "2"]
    assert result.calls == (("save", True), ("save", True))


async def test_timeout_while_settling_tools_drains_cleanup_and_never_calls_callback(
    monkeypatch, tmp_path
):
    cleaned, observed, workers = [], [], []

    @tool("slow", "an in-flight write", {})
    async def slow(args):
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.01)
            cleaned.append("drained")

    async def script(session):
        workers.append(asyncio.create_task(
            built.kwargs["server"].host_tool_table()["slow"].handler({})
        ))
        await asyncio.sleep(0)
        await finish(session)

    built = script_session(monkeypatch, script)
    tools = ToolSet(tools=[slow], grading=Grading(read=(), view=(), write=("slow",)), grant=("slow",))
    result = await asyncio.wait_for(run_prompt(
        "go", **make_args(tmp_path, tools=tools, timeout=0.05,
                          on_turn_result=observed.append, max_followups=2)
    ), 2)
    await asyncio.gather(*workers, return_exceptions=True)
    assert result.error == "timeout"
    assert observed == []
    assert cleaned == ["drained"]
    assert built.session.closed == 1


async def test_callback_failure_ends_run_without_retry(monkeypatch, tmp_path):
    async def script(session):
        await finish(session)

    def on_turn(_):
        raise RuntimeError("submission observer failed")

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=on_turn, max_followups=2)
    )
    assert result.error == "RuntimeError: submission observer failed"
    assert result.stop_reason == "error"
    assert len(built.session.submitted_turns) == 1


async def test_incomplete_followup_does_not_reuse_previous_native_turn_count(
    monkeypatch, tmp_path
):
    async def script(session):
        if len(session.submitted_turns) == 1:
            session.last_result_num_turns = 3
            await finish(session)
        else:
            await asyncio.sleep(3600)

    script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, timeout=0.05, on_turn_result=lambda _: "correct",
                          max_followups=1)
    )
    assert result.error == "timeout"
    assert result.native_num_turns is None


async def test_native_turn_counts_are_per_turn_in_callback_and_summed_at_return(
    monkeypatch, tmp_path
):
    observed = []

    async def script(session):
        turn = len(session.submitted_turns)
        session.last_result_num_turns = turn + 1
        await finish(session, turn=turn)

    def on_turn(result):
        observed.append(result.native_num_turns)
        return "correct"

    script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, on_turn_result=on_turn, max_followups=1)
    )
    assert observed == [2, 3]
    assert result.native_num_turns == 5


async def test_callback_ignoring_cancellation_cannot_hold_timeout_or_submit_after_close(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(headless, "_DRAIN_TIMEOUT", 0.01)
    release = asyncio.Event()
    ended = asyncio.Event()

    async def script(session):
        await finish(session)

    async def on_turn(_):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
        ended.set()
        return "must not be submitted"

    built = script_session(monkeypatch, script)
    try:
        result = await asyncio.wait_for(run_prompt(
            "go", **make_args(tmp_path, timeout=0.05, on_turn_result=on_turn, max_followups=2)
        ), 2)
        assert result.error == "timeout"
        assert built.session.closed == 1
    finally:
        release.set()
        await asyncio.wait_for(ended.wait(), 2)
    assert len(built.session.submitted_turns) == 1
