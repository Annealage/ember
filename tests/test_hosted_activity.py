"""Hosted activity reports real turn events, not viewer or timer traffic."""

import time
from types import SimpleNamespace

import pytest
from conftest import create_toy_app
from test_hosted_authority_ws import _send, _Verifier
from test_hosted_session import _build

from annealage_ember import app as app_module
from annealage_ember.session.base import TextDelta, ThinkingDelta, ToolResult, ToolUse, TurnEnd

pytestmark = pytest.mark.asyncio


async def test_hosted_activity_reports_only_real_active_turn_events(
    served_dir, tmp_path_factory, monkeypatch
):
    progress = []
    hosted = _build(
        served_dir, tmp_path_factory, verifier=_Verifier(), activity_sink=progress.append
    )
    app = hosted.app
    await app.agent_start()
    held = None
    try:
        (session,) = hosted.sessions
        clock = [100.0]
        monkeypatch.setattr(
            app_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        )
        session.emit(TextDelta(turn=1, text="not active"))
        app.agent_holder.on_presence(1)
        assert progress == []
        held = await _send(app, "alice", "start")
        session.emit(TextDelta(turn=1, text="private text"))
        session.emit(TextDelta(turn=1, text="coalesced"))
        assert progress == [{"kind": "turn_started", "turn": 1}, {"kind": "text_delta", "turn": 1}]
        clock[0] += 1
        session.emit(ThinkingDelta(turn=1, text="private thought"))
        clock[0] += 1
        session.emit(ToolUse(turn=1, tool_use_id="call", name="secret", input={"secret": 1}))
        clock[0] += 1
        session.emit(ToolResult(tool_use_id="unknown", is_error=False, text="not ours"))
        session.emit(ToolResult(tool_use_id="call", is_error=False, text="private result"))
        assert progress == [
            {"kind": "turn_started", "turn": 1},
            {"kind": "text_delta", "turn": 1},
            {"kind": "thinking_delta", "turn": 1},
            {"kind": "tool_use", "turn": 1, "tool_use_id": "call"},
            {"kind": "tool_result", "turn": 1, "tool_use_id": "call"},
        ]
        session.emit(ToolUse(turn=1, tool_use_id="late-call", name="secret", input={}))
        session.emit(TurnEnd(turn=1, stop_reason="end_turn", cost_usd=None))
        session.emit(ToolResult(tool_use_id="late-call", is_error=False, text="late result"))
        clock[0] += 2
        session.emit(TextDelta(turn=1, text="late"))
        app.agent_holder.on_presence(0)
        assert progress[-3:] == [
            {"kind": "tool_use", "turn": 1, "tool_use_id": "late-call"},
            {"kind": "turn_finished", "turn": 1},
            {"kind": "tool_result", "turn": 1, "tool_use_id": "late-call"},
        ]
        assert len(progress) == 8
    finally:
        if held is not None:
            await held.close()
        await app.agent_stop()


async def test_a_failed_activity_sink_does_not_stop_the_turn(served_dir, tmp_path_factory):
    def fail(_kind):
        raise RuntimeError("private failure")

    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier(), activity_sink=fail)
    app = hosted.app
    await app.agent_start()
    held = None
    try:
        held = await _send(app, "alice", "start")
        (session,) = hosted.sessions
        session.emit(TextDelta(turn=1, text="still running"))
        assert app.agent_holder.turn_in_flight()
        session.emit(TurnEnd(turn=1, stop_reason="end_turn", cost_usd=None))
        assert not app.agent_holder.turn_in_flight()
    finally:
        if held is not None:
            await held.close()
        await app.agent_stop()


async def test_activity_sink_requires_hosted_mode(served_dir):
    with pytest.raises(ValueError, match="activity_sink.*requires hosted mode"):
        create_toy_app(served_dir, token="t", activity_sink=lambda event: None)
