"""The server's clock on the events a page shows a time for (``at``), and the
drop of a resent ``turn`` whose ``client_id`` the log already holds.

``at`` is stamped once, when ``UserTurn``, ``TurnEnd``, ``AgentClosed`` or any
other feed item (deltas, tool calls and results, permission requests, upload
actions, agent errors) is built, so the persisted line and the live frame (each
a ``to_wire()`` of the same event) agree, and a log written before it existed
still loads.

The dedupe is what makes a page's single resend of an unconfirmed message
safe: the first copy may have arrived after all, and the replay carries it.
``_dispatch`` is called directly, as ``test_turn_frames.py`` does.
"""

import asyncio
import json
import time

import pytest

from annealage_ember import app as app_module
from annealage_ember.http import ws as ws_module
from annealage_ember.session.base import (
    AGENT_READY,
    AGENT_UNAVAILABLE,
    AgentClosed,
    AgentError,
    PermissionRequest,
    SessionReset,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnEnd,
    UploadActionEnded,
    UserTurn,
)
from annealage_ember.session.events import EventLog, _coalesce, read_records
from annealage_ember.session.fake import FakeSession
from annealage_ember.viewers import ViewerBus

pytestmark = pytest.mark.asyncio


class RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class StubRegistry:
    def __init__(self):
        self.broadcasts = []

    async def touch(self, conn):
        pass

    async def broadcast(self, frame):
        self.broadcasts.append(frame)


class _Conn:
    tab_id = "tab-1"
    human = None


def _bus():
    return ViewerBus(None, url="http://127.0.0.1:8765/")


def _logged(log, kind):
    return [wire for _seq, wire in log.replay(0).events if wire["kind"] == kind]


async def _send(session, bus, log, text, client_id, **extra):
    frame = {"v": 1, "type": "turn", "blocks": [{"type": "text", "text": text}]}
    if client_id is not None:
        frame["client_id"] = client_id
    frame.update(extra)
    sock = RecordingSocket()
    await ws_module._dispatch(sock, _Conn(), StubRegistry(), log, "tok", frame, session, bus)
    return sock.sent


# -- at ---------------------------------------------------------------------------


def _events():
    return [
        UserTurn(turn=1, blocks=[{"type": "text", "text": "hi"}], client_id="c-1"),
        ToolUse(turn=1, tool_use_id="t1", name="read", input={}),
        ToolResult(tool_use_id="t1", is_error=False, text="ok"),
        TurnEnd(turn=1, stop_reason="end", cost_usd=None),
        AgentClosed(reason="idle"),
    ]


@pytest.mark.parametrize(
    "make",
    [
        lambda: UserTurn(turn=1, blocks=[], client_id="c-1"),
        lambda: TurnEnd(turn=1, stop_reason="end", cost_usd=None),
        lambda: AgentClosed(reason="idle"),
        lambda: TextDelta(turn=1, text="hi"),
        lambda: ThinkingDelta(turn=1, text="hm"),
        lambda: ToolUse(turn=1, tool_use_id="t1", name="read", input={}),
        lambda: ToolResult(tool_use_id="t1", is_error=False, text="ok"),
        lambda: PermissionRequest(request_id="r1", tool="read", input={}),
        lambda: UploadActionEnded(
            id="a1", label="L", file="f", bytes=1, tool="t", outcome="done", text=""
        ),
        lambda: AgentError(stderr="", remediation="boom"),
    ],
    ids=[
        "user_turn",
        "turn_end",
        "agent_closed",
        "text_delta",
        "thinking_delta",
        "tool_use",
        "tool_result",
        "permission_request",
        "upload_action",
        "agent_error",
    ],
)
async def test_at_is_epoch_milliseconds_taken_when_the_event_is_built(make):
    before = time.time_ns() // 1_000_000
    event = make()
    after = time.time_ns() // 1_000_000
    assert isinstance(event.at, int) and before <= event.at <= after
    # Stamped once: asking for the wire again never re-stamps.
    await asyncio.sleep(0.003)
    assert event.to_wire()["at"] == event.to_wire()["at"] == event.at


async def test_the_persisted_line_and_the_live_frame_carry_the_same_at(tmp_path):
    path = tmp_path / "events.jsonl"
    log, registry = EventLog(str(path)), StubRegistry()
    publish = app_module._event_publisher(registry, log)
    events = _events()
    for event in events:
        publish(event)
    await asyncio.sleep(0)
    log.close()

    persisted = [record["event"] for record in read_records(path)]
    live = [frame["event"] for frame in registry.broadcasts]
    assert [wire["at"] for wire in persisted] == [event.at for event in events]
    assert live == persisted


async def test_the_ws_publisher_carries_the_same_at_on_the_log_and_the_frame():
    log, registry = EventLog(), StubRegistry()
    event = UserTurn(turn=1, blocks=[], client_id="c-1")
    ws_module._publish(log, registry, event)
    await asyncio.sleep(0)
    ((_seq, logged),) = log.replay(0).events
    assert logged["at"] == registry.broadcasts[0]["event"]["at"] == event.at


async def test_a_log_written_before_at_existed_loads_and_replays_unchanged(tmp_path):
    path = tmp_path / "events.jsonl"
    old = [
        {
            "kind": "user_turn",
            "turn": 1,
            "blocks": [{"type": "text", "text": "hi"}],
            "client_id": "c-old",
        },
        {"kind": "turn_end", "turn": 1, "stop_reason": "end"},
        {"kind": "agent_closed", "reason": "idle"},
    ]
    path.write_text(
        "".join(json.dumps({"seq": i + 1, "event": e}) + "\n" for i, e in enumerate(old))
    )

    log = EventLog(str(path))
    assert [wire for _seq, wire in log.replay(0).events] == old
    assert all("at" not in wire for wire in old)
    # What was appended after the restart is stamped; what was there is left alone.
    log.append(TurnEnd(turn=2, stop_reason="end", cost_usd=None))
    log.close()
    records = [record["event"] for record in read_records(path)]
    assert records[:3] == old
    assert isinstance(records[3]["at"], int)


async def test_a_coalesced_run_of_deltas_keeps_the_time_of_its_first_piece():
    records = [
        (1, {"kind": "text_delta", "turn": 1, "text": "a", "at": 10}),
        (2, {"kind": "text_delta", "turn": 1, "text": "b", "at": 20}),
        (3, {"kind": "thinking_delta", "turn": 1, "text": "c", "at": 30}),
        (4, {"kind": "thinking_delta", "turn": 1, "text": "d", "at": 40}),
    ]
    assert _coalesce(records) == [
        (2, {"kind": "text_delta", "turn": 1, "text": "ab", "at": 10}),
        (4, {"kind": "thinking_delta", "turn": 1, "text": "cd", "at": 30}),
    ]


# -- dedupe ------------------------------------------------------------------------


async def test_a_resent_turn_with_a_logged_client_id_is_dropped_without_an_error(capsys):
    log = EventLog()
    session = FakeSession(lambda event: None)
    bus = _bus()

    assert await _send(session, bus, log, "carry on", "c-1") == []
    capsys.readouterr()
    assert await _send(session, bus, log, "carry on", "c-1") == []

    assert [wire["client_id"] for wire in _logged(log, "user_turn")] == ["c-1"]
    assert len(session.submitted_turns) == 1
    assert bus.turn == 1
    err = capsys.readouterr().err
    assert "turn received client_id=c-1 outcome=duplicate" in err
    assert "turn logged" not in err


async def test_the_duplicate_is_dropped_before_the_admission_checks():
    """A resend arrives while the agent is busy or the session is not taking
    turns: the refusal the gate would give must not reach the page, since the
    message it names is already in the replay."""
    log = EventLog()
    session = FakeSession(lambda event: None)
    bus = _bus()
    assert await _send(session, bus, log, "first", "c-1") == []

    session.set_status(AGENT_UNAVAILABLE)
    assert await _send(session, bus, log, "first", "c-1") == []
    # A different message is still refused, so the gate was in play.
    refused = await _send(session, bus, log, "other", "c-2")
    assert [(frame["type"], frame.get("client_id")) for frame in refused] == [("refused", "c-2")]


async def test_a_turn_without_a_client_id_is_never_a_duplicate():
    log = EventLog()
    session = FakeSession(lambda event: None)
    bus = _bus()
    assert await _send(session, bus, log, "same words", None) == []
    assert await _send(session, bus, log, "same words", None) == []
    assert [wire["turn"] for wire in _logged(log, "user_turn")] == [1, 2]


async def test_a_redelivery_with_the_same_client_id_is_not_dropped():
    log = EventLog()
    session = FakeSession(lambda event: None)
    bus = _bus()
    assert await _send(session, bus, log, "carry on", "c-1") == []
    assert await _send(session, bus, log, "carry on", "c-1", redelivery_of=1) == []

    assert [wire["turn"] for wire in _logged(log, "user_turn")] == [1, 2]
    assert len(session.submitted_turns) == 2


async def test_the_holders_own_redelivery_is_unaffected_by_the_index():
    """``AgentHolder._redeliver`` builds its ``UserTurn`` itself, with the
    original's ``client_id``; the log takes it and the index still names the id."""
    log = EventLog()
    log.append(UserTurn(turn=1, blocks=[], client_id="c-1"))
    log.append(UserTurn(turn=2, blocks=[], client_id="c-1", redelivery_of=1))
    assert [wire["turn"] for wire in _logged(log, "user_turn")] == [1, 2]
    assert log.has_user_turn("c-1")


async def test_a_refused_turns_client_id_is_not_indexed_so_it_can_be_sent_again(capsys):
    log = EventLog()
    session = FakeSession(lambda event: None)
    session.set_status(AGENT_UNAVAILABLE)
    bus = _bus()

    refused = await _send(session, bus, log, "hello", "c-7")
    assert [(frame["type"], frame.get("client_id")) for frame in refused] == [("refused", "c-7")]
    assert not log.has_user_turn("c-7")
    assert "turn received client_id=c-7 outcome=refused:" in capsys.readouterr().err

    session.set_status(AGENT_READY)
    assert await _send(session, bus, log, "hello", "c-7") == []
    assert [wire["client_id"] for wire in _logged(log, "user_turn")] == ["c-7"]
    assert len(session.submitted_turns) == 1


async def test_the_index_is_rebuilt_when_an_existing_log_is_loaded(tmp_path):
    path = tmp_path / "events.jsonl"
    first = EventLog(str(path))
    first.append(UserTurn(turn=1, blocks=[], client_id="c-1"))
    first.append(TurnEnd(turn=1, stop_reason="end", cost_usd=None))
    first.close()

    log = EventLog(str(path))
    assert log.has_user_turn("c-1") and not log.has_user_turn("c-2")
    session = FakeSession(lambda event: None)
    assert await _send(session, _bus(), log, "again", "c-1") == []
    assert session.submitted_turns == []


async def test_a_session_reset_forgets_the_ids_it_ended():
    log = EventLog()
    log.append(UserTurn(turn=1, blocks=[], client_id="c-1"))
    log.append(SessionReset(reason="resume failed"))
    assert not log.has_user_turn("c-1")


# -- hop log -----------------------------------------------------------------------


async def test_the_hop_lines_name_the_client_id_and_never_the_message(capsys):
    log = EventLog()
    session = FakeSession(lambda event: None)
    await _send(session, _bus(), log, "a private sentence", "c-9")

    err = capsys.readouterr().err
    assert "info: turn received client_id=c-9 outcome=admitted" in err
    assert "info: turn logged client_id=c-9 turn=1" in err
    assert "private" not in err
