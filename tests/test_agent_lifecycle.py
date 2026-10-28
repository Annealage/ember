"""The agent belongs to the project, not to a page.

A page connecting neither starts the agent nor keeps it open; an interaction
starts it (resumed), and it closes after ``idle_timeout`` with no turn
running or queued and no permission request open. An input accepted while it
starts is kept for it, never refused. One live agent per project: a second
open is refused, and an orphan a killed server left is ended, not an obstacle.
What an earlier process accepted but never gave its agent is sent again once,
and everything else it left running is ended as interrupted.
"""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from conftest import create_toy_app, make_test_client

from annealage_ember import agent_lock, sessions
from annealage_ember.http import ws as ws_module
from annealage_ember.session.base import (
    AGENT_CONNECTING,
    AGENT_READY,
    AGENT_UNAVAILABLE,
    AgentStatus,
    InputState,
    TurnEnd,
    UserTurn,
)
from annealage_ember.session.events import EventLog
from annealage_ember.session.external import ExternalAgentSession
from annealage_ember.session.permissions import PermissionBroker

pytestmark = pytest.mark.asyncio

IDLE = 0.15


class _Session(ExternalAgentSession):
    """A session that is connecting until its ``gate`` opens, then ready (or
    unavailable, when ``fails``), and keeps what it is given."""

    gate = None
    fails = False

    def __init__(self, on_event, broker):
        super().__init__(on_event, broker)
        self.emit = on_event
        self.status = AGENT_CONNECTING if type(self).gate is not None else AGENT_READY
        self.submitted = []
        self.closed = 0
        self.pid = None

    def agent_status(self):
        return self.status

    @property
    def process_id(self):
        return self.pid

    async def start(self):
        if type(self).gate is not None:
            await type(self).gate.wait()
        if type(self).fails:
            self.status = AGENT_UNAVAILABLE
            self.emit(AgentStatus(status=AGENT_UNAVAILABLE))
        else:
            self.status = AGENT_READY
            self.emit(AgentStatus(status=AGENT_READY))

    async def submit_turn(self, blocks, viewer=None):
        self.submitted.append(blocks)

    async def close(self):
        self.closed += 1
        await super().close()


@pytest.fixture(autouse=True)
def _reset_session_class():
    _Session.gate = None
    _Session.fails = False
    yield
    _Session.gate = None
    _Session.fails = False


def _app(served_dir, built, session_id=None, **kwargs):
    def build(on_event, *, bus):
        broker = PermissionBroker(on_event, viewer_url=bus.url, timeout=30)
        bus.broker = broker
        built.append(_Session(on_event, broker))
        return built[-1]

    kwargs.setdefault("resume_session", build)
    kwargs.setdefault("idle_timeout", IDLE)
    return create_toy_app(
        served_dir,
        token="tok",
        agent_token="agent-tok",
        session_id=session_id or sessions.create_session(served_dir),
        build_session=build,
        **kwargs,
    )


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


def _logged(app, kind):
    return [wire for _seq, wire in app.agent_event_log.replay(0).events if wire["kind"] == kind]


class _Conn:
    tab_id = "tab-1"
    human = None


class _Socket:
    def __init__(self):
        self.sent = []

    async def send(self, payload, opcode=None):
        self.sent.append(payload)


class _Registry:
    def __init__(self, real):
        self._real = real

    async def touch(self, conn):
        pass

    async def broadcast(self, frame):
        await self._real.broadcast(frame)


async def _send_turn(app, text, client_id="c-1"):
    """What a turn frame does after the holder opens the agent for it."""
    session = await app.agent_holder.ensure(retry=True)
    frame = {
        "v": 2,
        "type": "turn",
        "blocks": [{"type": "text", "text": text}],
        "client_id": client_id,
    }
    sock = _Socket()
    await ws_module._dispatch(
        sock,
        _Conn(),
        _Registry(app.agent_registry),
        app.agent_event_log,
        "tok",
        frame,
        session,
        app.agent_bus,
    )
    return sock.sent


# -- opening and closing ----------------------------------------------------


async def test_a_page_connecting_does_not_open_the_agent_and_an_interaction_does(served_dir):
    built = []
    app = _app(served_dir, built, start_closed=True, idle_timeout=None)
    await app.agent_start()
    try:
        client = make_test_client(app)
        await client.get(
            "/ws?t=tok",
            headers={
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            },
        )
        assert built == [] and app.agent_status()["agent"] == "closed"
        await app.agent_holder.ensure(retry=True)
        assert len(built) == 1 and app.agent_status()["agent"] == AGENT_READY
    finally:
        await app.agent_stop()


async def test_the_idle_sweep_keeps_an_agent_with_a_running_turn_or_an_open_request(served_dir):
    built = []
    app = _app(served_dir, built)
    await app.agent_start()
    try:
        session = built[0]
        session.emit(UserTurn(turn=1, blocks=[{"type": "text", "text": "go"}]))
        await asyncio.sleep(IDLE * 3)
        assert app.agent_status()["agent"] == AGENT_READY and session.closed == 0

        session.emit(TurnEnd(turn=1, stop_reason="end", cost_usd=0.0))
        await _until(lambda: app.agent_status()["agent"] == "closed")
        assert [e["reason"] for e in _logged(app, "agent_closed")] == ["idle"]
    finally:
        await app.agent_stop()


async def test_an_input_accepted_while_the_agent_starts_is_kept_and_delivered_when_ready(
    served_dir,
):
    built = []
    _Session.gate = asyncio.Event()
    app = _app(served_dir, built, start_closed=True, idle_timeout=None)
    await app.agent_start()
    try:
        assert await _send_turn(app, "carry on") == []
        (session,) = built
        assert session.agent_status() == AGENT_CONNECTING and session.submitted == []
        (turn,) = _logged(app, "user_turn")
        assert turn["turn"] == 1 and turn["client_id"] == "c-1"
        assert [(e["turn"], e["state"]) for e in _logged(app, "input_state")] == [(1, "queued")]

        # A second one waits behind it, in order, rather than jumping ahead.
        assert await _send_turn(app, "and then this", client_id="c-2") == []
        _Session.gate.set()
        await _until(lambda: len(session.submitted) == 2)
        assert [b[0]["text"] for b in session.submitted] == ["carry on", "and then this"]
    finally:
        await app.agent_stop()


async def test_an_input_waiting_for_an_agent_that_never_starts_is_rejected_with_a_reason(
    served_dir,
):
    built = []
    _Session.gate = asyncio.Event()
    _Session.fails = True
    app = _app(served_dir, built, start_closed=True, idle_timeout=None)
    await app.agent_start()
    try:
        await _send_turn(app, "carry on")
        _Session.gate.set()
        await _until(lambda: _logged(app, "turn_end"))
        assert [(e["turn"], e["stop_reason"]) for e in _logged(app, "turn_end")] == [
            (1, "rejected")
        ]
        rejected = [e for e in _logged(app, "input_state") if e["state"] == "rejected"]
        assert rejected and "did not start" in rejected[0]["detail"]
    finally:
        await app.agent_stop()


async def test_a_hosted_input_waiting_to_start_is_not_delivered_without_live_authority(
    served_dir,
):
    built = []
    _Session.gate = asyncio.Event()
    app = _app(served_dir, built, start_closed=True, idle_timeout=None)
    await app.agent_start()
    try:
        await _send_turn(app, "carry on")
        (session,) = built
        # Hosted mode, and the authority it was accepted under is gone by the
        # time the agent is up (expired, revoked, or another principal's now).
        app.agent_bus.hosted_mode = True
        app.agent_bus.hosted_turn_live = False
        _Session.gate.set()
        await _until(lambda: _logged(app, "turn_end"))
        assert session.submitted == []
        assert [(e["turn"], e["stop_reason"]) for e in _logged(app, "turn_end")] == [
            (1, "rejected")
        ]
        rejected = [e for e in _logged(app, "input_state") if e["state"] == "rejected"]
        assert rejected and "access ended" in rejected[0]["detail"]
    finally:
        await app.agent_stop()


# -- one agent per project ---------------------------------------------------


async def test_a_second_open_of_the_same_project_is_refused_until_the_first_closes(served_dir):
    first_built, second_built = [], []
    sid = sessions.create_session(served_dir)
    first = _app(served_dir, first_built, session_id=sid, idle_timeout=None)
    second = _app(served_dir, second_built, session_id=sid, idle_timeout=None)
    try:
        assert len(first_built) == 1 and second_built == []
        assert second.agent_status()["agent"] == "closed"
        errors = [e for e in _logged(second, "agent_error") if "already" in e["stderr"]]
        assert errors

        await second.agent_holder.ensure(retry=True)
        assert second_built == []

        await first.agent_stop()
        await second.agent_holder.ensure(retry=True)
        assert len(second_built) == 1
    finally:
        await first.agent_stop()
        await second.agent_stop()


async def test_a_live_holder_refuses_the_lock_and_a_dead_ones_does_not(tmp_path):
    held = agent_lock.AgentLock(tmp_path)
    held.acquire()
    other = agent_lock.AgentLock(tmp_path)
    with pytest.raises(agent_lock.AgentAlreadyOpen) as refused:
        other.acquire()
    assert refused.value.owner == os.getpid()
    held.release()
    other.acquire()
    other.release()


async def test_an_orphaned_agent_a_killed_server_left_is_ended_not_an_obstacle(tmp_path):
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        first = agent_lock.AgentLock(tmp_path)
        first.acquire()
        first.record_agent(orphan.pid)
        # The server is SIGKILLed: its flock goes with it, the record stays.
        record = (tmp_path / "agent.lock").read_text()
        first.release()
        (tmp_path / "agent.lock").write_text(record)
        assert json.loads(record)["agent"]["pid"] == orphan.pid

        successor = agent_lock.AgentLock(tmp_path)
        successor.acquire()
        assert successor.reaped == orphan.pid
        assert orphan.wait(timeout=10) is not None
        successor.release()
    finally:
        if orphan.poll() is None:
            orphan.kill()


async def test_a_recorded_agent_pid_that_is_now_some_other_process_is_left_alone(tmp_path):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stale = {
            "pid": 1,
            "agent": {"pid": bystander.pid, "start": 1, "cmd": "an omp that died long ago"},
        }
        (tmp_path / "agent.lock").write_text(json.dumps(stale))
        lock = agent_lock.AgentLock(tmp_path)
        lock.acquire()
        assert lock.reaped is None and bystander.poll() is None
        lock.release()
    finally:
        bystander.kill()
        bystander.wait()


# -- what an earlier process left ---------------------------------------------


def _seed(served_dir, *events):
    sid = sessions.create_session(served_dir)
    log = EventLog(str(sessions.events_path(served_dir, sid)))
    for event in events:
        log.append(event)
    log.close()
    return sid


async def test_boot_resends_an_input_the_agent_never_took_and_only_that(served_dir):
    blocks = [{"type": "text", "text": "never ran"}]
    sid = _seed(
        served_dir,
        UserTurn(turn=1, blocks=blocks, client_id="c-1", by="andrew@example"),
        InputState("a", 1, "received"),
        InputState("a", 1, "queued"),
        UserTurn(turn=2, blocks=[{"type": "text", "text": "was running"}]),
        InputState("b", 2, "received"),
        InputState("b", 2, "provider_ack"),
    )
    built = []
    app = _app(served_dir, built, session_id=sid, idle_timeout=None)
    ended = [(e["turn"], e["stop_reason"]) for e in _logged(app, "turn_end")]
    assert ended == [(1, "redelivered"), (2, "interrupted")]
    await app.agent_start()
    try:
        session = built[0]
        await _until(lambda: session.submitted)
        assert session.submitted == [blocks]
        resent = [e for e in _logged(app, "user_turn") if e.get("redelivery_of") == 1]
        assert len(resent) == 1 and resent[0]["turn"] == 3
        assert resent[0]["client_id"] == "c-1" and resent[0]["by"] == "andrew@example"
    finally:
        await app.agent_stop()


async def test_a_turn_already_resent_once_is_never_resent_again(served_dir):
    blocks = [{"type": "text", "text": "crashes the agent"}]
    sid = _seed(
        served_dir,
        UserTurn(turn=1, blocks=blocks),
        TurnEnd(turn=1, stop_reason="redelivered", cost_usd=None),
        UserTurn(turn=2, blocks=blocks, redelivery_of=1),
        InputState("a", 2, "received"),
    )
    built = []
    app = _app(served_dir, built, session_id=sid, idle_timeout=None)
    assert [(e["turn"], e["stop_reason"]) for e in _logged(app, "turn_end")][-1] == (
        2,
        "interrupted",
    )
    await app.agent_start()
    try:
        await asyncio.sleep(0.1)
        assert built[0].submitted == []
    finally:
        await app.agent_stop()
