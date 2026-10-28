"""The hosted turn authority through a real ``/ws`` and a real app over a stand-in backend."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from conftest import make_test_client
from microdot.microdot import Request
from microdot.websocket import WebSocket
from test_hosted_session import OTHER, PRINCIPAL, _build
from test_identity import HELLO, UPGRADE, _RawSock, _turn

from annealage_ember import protocol

pytestmark = pytest.mark.asyncio


class _Verifier:
    """Two principals, and a delegation that outlives the test or does not."""

    def __init__(self, expiry=None):
        self.expiry = expiry

    def verify(self, token, operation):
        subs = {"alice": PRINCIPAL, "bob": OTHER}
        if token not in subs:
            raise ValueError("invalid delegation")
        return SimpleNamespace(
            sub=subs[token],
            wsp="wsp_test",
            prj="prj_test",
            ops=("project.read", "project.exec"),
            job="job_test",
            rev="a" * 40,
            exp=self.expiry if self.expiry is not None else time.time() + 60,
        )


def _as(who):
    return {"Authorization": "Bearer " + who, "X-Annealage-Principal-Display": who}


class _Held:
    """One open ``/ws`` connection that stays open until ``close()``: the
    server only sees the peer go away then, as it does when the front closes it."""

    def __init__(self, app, who):
        self.app, self.who = app, who
        self.gone = asyncio.Event()
        self.task = None

    async def open(self, frames):
        client = make_test_client(self.app)
        request = client._render_request("GET", "/ws", {**UPGRADE, **_as(self.who)}, b"")
        inbound = b"".join(
            bytes(
                WebSocket._encode_websocket_frame(
                    WebSocket.TEXT, json.dumps({"v": protocol.PROTOCOL_VERSION, **frame})
                )
            )
            for frame in frames
        )
        gone = self.gone

        class Sock(_RawSock):
            async def read(self, n):
                if not self.buffer:
                    await gone.wait()
                    raise ConnectionError("peer closed")
                return await super().read(n)

        sock = Sock(request + inbound)
        req = await Request.create(self.app, sock, sock, ("127.0.0.1", 1234), scheme=None)
        self.task = asyncio.ensure_future(self.app.dispatch_request(req))
        await asyncio.sleep(0.2)
        return self

    async def close(self):
        self.gone.set()
        await asyncio.wait_for(self.task, 5)


async def _send(app, who, text, client_id="c-1"):
    """A turn sent on a connection that is still open afterwards."""
    held = _Held(app, who)
    return await held.open([HELLO, {**_turn(text), "client_id": client_id}])


def _logged(app, kind):
    return [w for _s, w in app.agent_event_log.replay(0).events if w["kind"] == kind]


async def test_another_principals_turn_is_refused_and_changes_nothing(served_dir, tmp_path_factory):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        await _send(app, "alice", "first")
        (session,) = hosted.sessions
        bus = app.agent_bus
        before = (bus.hosted_turn_principal, bus.hosted_turn_ops, bus.hosted_turn_secret)
        assert before[0] == PRINCIPAL and bus.hosted_turn_live

        await _send(app, "bob", "second", client_id="c-2")
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["first"]
        assert (bus.hosted_turn_principal, bus.hosted_turn_ops, bus.hosted_turn_secret) == before
        assert [w["client_id"] for w in _logged(app, "user_turn")] == ["c-1"]
    finally:
        await app.agent_stop()


async def test_the_owner_cannot_queue_or_steer_behind_their_own_running_turn(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        await _send(app, "alice", "first")
        await _send(app, "alice", "steer", client_id="c-2")
        (session,) = hosted.sessions
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["first"]
    finally:
        await app.agent_stop()


async def test_a_turn_still_running_when_its_delegation_expires_is_interrupted(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier(expiry=time.time() + 0.5))
    app = hosted.app
    await app.agent_start()
    try:
        await _send(app, "alice", "first")
        (session,) = hosted.sessions
        bus = app.agent_bus
        assert session.interrupted == 0 and bus.hosted_turn_live
        await asyncio.sleep(0.8)
        assert session.interrupted == 1
        assert not bus.hosted_turn_live and bus.hosted_turn_ops == ()
        # Expired authority cannot start another turn either.
        await _send(app, "alice", "later", client_id="c-2")
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["first"]
    finally:
        await app.agent_stop()


async def test_losing_the_connection_that_holds_the_grant_ends_it_and_interrupts(
    served_dir, tmp_path_factory
):
    """The front closes the socket when access is revoked: that is all this
    process is told, so the grant ends with it and the turn stops."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        held = await _send(app, "alice", "first")
        (session,) = hosted.sessions
        bus = app.agent_bus
        assert bus.hosted_turn_live and session.interrupted == 0
        await held.close()
        await asyncio.sleep(0.1)
        assert not bus.hosted_turn_live and bus.hosted_turn_ops == ()
        assert bus.hosted_turn_secret is None
        assert session.interrupted == 1
    finally:
        await app.agent_stop()


async def test_a_prompt_waiting_for_the_agent_to_start_is_not_dispatched_after_revocation(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        session.set_status("connecting")
        held = await _send(app, "alice", "wait for me")
        assert session.submitted_turns == []
        assert any(w["state"] == "queued" for w in _logged(app, "input_state"))
        await held.close()  # revoked while the agent is still starting
        await asyncio.sleep(0.1)
        session.set_status("ready")
        await asyncio.sleep(0.5)
        assert session.submitted_turns == []
        assert [w["stop_reason"] for w in _logged(app, "turn_end")] == ["rejected"]
    finally:
        await app.agent_stop()
