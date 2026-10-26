"""Behavioural coverage for recent-turn history, paging and live catchup."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from annealage_ember import protocol
from annealage_ember.http import ws as ws_module
from annealage_ember.session.base import TextDelta, TurnEnd, UserTurn
from annealage_ember.session.events import EventLog
from annealage_ember.viewers import ViewerRegistry


class Wire:
    def __init__(self, kind, **fields):
        self.event = dict(kind=kind, **fields)

    def to_wire(self):
        return self.event


def turn(log, number, chunks=2):
    log.append(UserTurn(turn=number, blocks=[{"type": "text", "text": str(number)}]))
    for chunk in range(chunks):
        log.append(TextDelta(turn=number, text="%d:%d " % (number, chunk)))
    log.append(TurnEnd(turn=number, stop_reason="end", cost_usd=0.0))


def turns(page):
    return [event["turn"] for _, event in page.events if event["kind"] == "user_turn"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_recent_and_older_pages_are_complete_ordered_and_nonoverlapping(tmp_path, restart):
    path = str(tmp_path / "events.jsonl")
    log = EventLog(path)
    for number in range(1, 66):
        turn(log, number, chunks=12)
    if restart:
        log.close()
        log = EventLog(path)
    try:
        page = await log.history_async()
        assert turns(page) == list(range(46, 66))
        assert page.through == log.current_seq
        pages = [page]
        while page.has_more:
            page = await log.history_async(page.before)
            pages.append(page)
        assert [turns(page) for page in pages] == [
            list(range(46, 66)),
            list(range(26, 46)),
            list(range(6, 26)),
            list(range(1, 6)),
        ]
        ordered = [record for page in reversed(pages) for record in page.events]
        seqs = [seq for seq, _ in ordered]
        assert seqs == sorted(set(seqs))
        assert sum(event["kind"] == "turn_end" for _, event in ordered) == 65
        assert all(
            event["text"].endswith(":11 ") for _, event in ordered if event["kind"] == "text_delta"
        )
        assert pages[-1].before == 1
        assert not pages[-1].has_more
    finally:
        log.close()


@pytest.mark.asyncio
async def test_overlapping_queued_turns_and_late_events_never_split_a_turn():
    log = EventLog()
    log.append(UserTurn(turn=1, blocks=[]))
    log.append(UserTurn(turn=2, blocks=[]))
    log.append(TextDelta(turn=1, text="running while input two queues"))
    log.append(TurnEnd(turn=1, stop_reason="end", cost_usd=0))
    log.append(TextDelta(turn=2, text="reply two"))
    log.append(TurnEnd(turn=2, stop_reason="end", cost_usd=0))
    turn(log, 3)
    recent = await log.history_async(limit=1)
    assert turns(recent) == [3]
    older = await log.history_async(recent.before, limit=1)
    assert turns(older) == [1, 2]
    assert not older.has_more
    # A late event for an already-ended turn merges its crossed boundaries.
    log.append(TextDelta(turn=2, text="late result"))
    assert turns(await log.history_async(limit=1)) == [1, 2, 3]


@pytest.mark.asyncio
async def test_reset_floor_and_reused_turn_numbers_survive_restart(tmp_path):
    path = str(tmp_path / "events.jsonl")
    log = EventLog(path)
    for number in range(1, 4):
        turn(log, number)
    log.append(Wire("permission_request", request_id="pre-reset", tool="write", input={}))
    floor = log.append(Wire("session_reset"))
    for number in range(1, 4):
        turn(log, number)
    log.close()
    log = EventLog(path)
    try:
        page = await log.history_async(limit=2)
        assert turns(page) == [2, 3]
        older = await log.history_async(page.before)
        assert turns(older) == [1]
        assert older.before == floor
        assert not older.has_more
        assert older.events[0] == (floor, {"kind": "session_reset"})
        assert not page.pending_request_ids
        assert log.last_turn == 3
        assert log.unresolved_requests == ()
        with pytest.raises(ValueError, match="current conversation"):
            await log.history_async(floor - 1)
    finally:
        log.close()


@pytest.mark.asyncio
async def test_initial_page_includes_only_still_pending_older_approvals(tmp_path):
    log = EventLog(str(tmp_path / "events.jsonl"))
    try:
        first = log.append(Wire("permission_request", request_id="active", tool="write", input={}))
        log.append(Wire("permission_request", request_id="resolved", tool="write", input={}))
        log.append(Wire("permission_resolved", request_id="resolved", outcome="allow_once"))
        for number in range(1, 26):
            turn(log, number)
        page = await log.history_async()
        assert turns(page) == list(range(6, 26))
        assert page.pending_request_ids == ("active",)
        assert page.events[0][0] == first < page.before
        assert [
            event["request_id"] for _, event in page.events if event["kind"] == "permission_request"
        ] == ["active"]
        older = await log.history_async(page.before)
        assert older.pending_request_ids == ()
        assert any(event.get("request_id") == "resolved" for _, event in older.events)
    finally:
        log.close()


@pytest.mark.asyncio
async def test_file_page_seeks_to_index_and_captures_snapshot_off_event_loop(tmp_path, monkeypatch):
    path = str(tmp_path / "events.jsonl")
    log = EventLog(path)
    for number in range(1, 66):
        turn(log, number, chunks=12)
    log.close()
    log = EventLog(path)
    read = log._read_history
    entered, release = threading.Event(), threading.Event()
    offsets = []
    main_thread = threading.get_ident()

    def blocked(offset, lower, upper):
        assert threading.get_ident() != main_thread
        offsets.append(offset)
        entered.set()
        assert release.wait(5)
        return read(offset, lower, upper)

    monkeypatch.setattr(log, "_read_history", blocked)
    task = asyncio.create_task(log.history_async(limit=2))
    try:
        await asyncio.to_thread(entered.wait, 5)
        snapshot = log.current_seq
        turn(log, 66)
        release.set()
        page = await task
        assert offsets[0] > 0
        assert page.through == snapshot
        assert turns(page) == [64, 65]
        catchup = await log.replay_async(page.through)
        assert [event["turn"] for _, event in catchup.events if event["kind"] == "user_turn"] == [
            66
        ]
    finally:
        release.set()
        log.close()


class Socket:
    def __init__(self, hello=None, on_send=None):
        self.inbound = [json.dumps(hello)] if hello is not None else []
        self.sent = []
        self.on_send = on_send

    async def receive(self):
        return self.inbound.pop(0)

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        if self.on_send is not None:
            await self.on_send(frame)


def hello(**fields):
    return {"v": protocol.PROTOCOL_VERSION, "type": "hello", "token": "tok", **fields}


@pytest.mark.asyncio
async def test_first_greeting_is_bounded_and_catches_events_emitted_during_send():
    log = EventLog()
    for number in range(1, 26):
        turn(log, number)

    async def append_during_send(frame):
        if frame["type"] == "history_page":
            log.append(TextDelta(turn=26, text="arrived before registration"))

    sock = Socket(hello(history_turns=20), append_during_send)
    await ws_module._greet(sock, log, "tok", "s")
    page, event = sock.sent
    assert page["type"] == "history_page" and page["initial"]
    assert [
        item["event"]["turn"] for item in page["events"] if item["event"]["kind"] == "user_turn"
    ] == list(range(6, 26))
    assert event["type"] == "event"
    assert event["seq"] > page["through"]
    assert event["event"]["text"] == "arrived before registration"


@pytest.mark.asyncio
async def test_registration_follows_final_snapshot_without_losing_new_broadcast():
    log = EventLog()
    turn(log, 1)
    sock = Socket(hello(history_turns=20))
    registry = ViewerRegistry(event_log=log)
    await ws_module._greet(sock, log, "tok", "s")
    conn = await registry.add(sock)
    try:
        event = UserTurn(turn=2, blocks=[{"type": "text", "text": "registered live"}])
        seq = log.append(event)
        await registry.broadcast(protocol.build_event(seq, event.to_wire()))
        await asyncio.sleep(0)
        assert any(frame.get("seq") == seq for frame in sock.sent)
    finally:
        await registry.remove(conn)


@pytest.mark.asyncio
async def test_same_session_reconnect_uses_all_catchup_and_old_clients_keep_full_replay():
    log = EventLog()
    for number in range(1, 31):
        turn(log, number)
    reconnect = Socket(hello(history_turns=20, session_id="s", last_seq=4))
    await ws_module._greet(reconnect, log, "tok", "s")
    assert all(frame["type"] == "event" for frame in reconnect.sent)
    assert [
        frame["event"]["turn"] for frame in reconnect.sent if frame["event"]["kind"] == "user_turn"
    ] == list(range(2, 31))
    old = Socket(hello())
    await ws_module._greet(old, log, "tok", "s")
    assert sum(frame["event"]["kind"] == "user_turn" for frame in old.sent) == 30
    other = Socket(hello(history_turns=20, session_id="other", last_seq=log.current_seq))
    await ws_module._greet(other, log, "tok", "s")
    assert other.sent[0]["type"] == "history_page"


@pytest.mark.asyncio
async def test_history_dispatch_rejects_other_session_and_returns_matching_cursor():
    log = EventLog()
    for number in range(1, 26):
        turn(log, number)
    recent = await log.history_async()
    sock = Socket()
    frame = {"type": "history", "session_id": "other", "before": recent.before, "limit": 20}
    await ws_module._dispatch(sock, None, None, log, None, frame, history_session_id="s")
    assert sock.sent[-1]["type"] == "history_error"
    assert "events" not in sock.sent[-1]
    frame["session_id"] = "s"
    await ws_module._dispatch(sock, None, None, log, None, frame, history_session_id="s")
    assert sock.sent[-1]["requested_before"] == recent.before
    assert sock.sent[-1]["initial"] is False
    frame["before"] = None
    await ws_module._dispatch(sock, None, None, log, None, frame, history_session_id="s")
    assert sock.sent[-1]["initial"] is True
    assert "pending_request_ids" in sock.sent[-1]


@pytest.mark.asyncio
async def test_initial_history_failure_preserves_pending_and_can_retry(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(str(path))
    log.append(Wire("permission_request", request_id="active", tool="write", input={}))
    for number in range(1, 50):
        turn(log, number, chunks=12)
    content = path.read_bytes()
    log.close()
    log = EventLog(str(path))
    path.unlink()
    sock = Socket(hello(history_turns=20))
    try:
        await ws_module._greet(sock, log, "tok", "s")
        pending, error = sock.sent
        assert pending["pending_request_ids"] == ["active"]
        assert pending["events"][0]["event"]["request_id"] == "active"
        assert error["type"] == "history_error" and error["initial"]
        assert error["through"] == log.current_seq
        path.write_bytes(content)
        frame = {"type": "history", "session_id": "s", "before": None}
        await ws_module._dispatch(sock, None, None, log, None, frame, history_session_id="s")
        assert sock.sent[-1]["type"] == "history_page"
        assert (
            len([item for item in sock.sent[-1]["events"] if item["event"]["kind"] == "user_turn"])
            == 20
        )
    finally:
        log.close()


@pytest.mark.asyncio
async def test_history_requires_only_hosted_project_read():
    class End(Exception):
        pass

    class OneFrame(Socket):
        async def receive(self):
            if not self.inbound:
                raise End
            return await super().receive()

    frame = {"v": protocol.PROTOCOL_VERSION, "type": "history", "session_id": "s", "before": None}
    sock = OneFrame(frame)
    operations = []
    auth = SimpleNamespace(
        hosted_mode=True,
        permits=lambda human, operation: (
            operations.append(operation) or operation == "project.read"
        ),
        expired=lambda human: False,
    )
    human = SimpleNamespace(hosted_claims=SimpleNamespace(exp=10**12))
    holder = SimpleNamespace(session=None)
    with pytest.raises(End):
        await ws_module._serve_connection(
            sock, None, None, EventLog(), None, holder, auth, human, history_session_id="s"
        )
    assert operations == ["project.read"]
    assert sock.sent[-1]["type"] == "history_page"


@pytest.mark.parametrize("value", [0, 51, True, "20", 1.5, None])
def test_invalid_history_limits_are_refused(value):
    assert not protocol.validate_inbound(hello(history_turns=value))[0]
    frame = {
        "v": protocol.PROTOCOL_VERSION,
        "type": "history",
        "session_id": "s",
        "before": 1,
        "limit": value,
    }
    assert not protocol.validate_inbound(frame)[0]


@pytest.mark.parametrize("before", [0, -1, True, "1", 1.5])
def test_invalid_history_cursors_are_refused(before):
    frame = {"v": protocol.PROTOCOL_VERSION, "type": "history", "session_id": "s", "before": before}
    assert not protocol.validate_inbound(frame)[0]


@pytest.mark.asyncio
async def test_late_tool_result_remains_with_its_original_turn():
    log = EventLog()
    log.append(UserTurn(turn=1, blocks=[]))
    log.append(Wire("tool_use", turn=1, tool_use_id="tool-1", name="write", input={}))
    log.append(TurnEnd(turn=1, stop_reason="end", cost_usd=0))
    turn(log, 2)
    log.append(Wire("tool_result", tool_use_id="tool-1", is_error=False, text="done"))
    page = await log.history_async(limit=1)
    assert turns(page) == [1, 2]
    assert page.events[-1][1]["kind"] == "tool_result"
    assert not page.has_more


@pytest.mark.asyncio
async def test_no_turn_history_is_empty_except_for_active_permissions():
    log = EventLog()
    for _ in range(600):
        log.append(Wire("review_changed"))
    log.append(Wire("permission_request", request_id="active", tool="write", input={}))
    page = await log.history_async()
    assert len(page.events) == 1
    assert page.pending_request_ids == ("active",)
    assert page.before is None
    assert not page.has_more


@pytest.mark.asyncio
async def test_issued_cursor_survives_a_late_event_merging_newer_turn_groups():
    log = EventLog()
    for number in range(1, 4):
        turn(log, number)
    recent = await log.history_async(limit=1)
    log.append(TextDelta(turn=1, text="late live continuation"))
    older = await log.history_async(recent.before, limit=1)
    assert all(seq < recent.before for seq, _ in older.events)
    assert turns(older) == [1, 2]
    assert not older.has_more
