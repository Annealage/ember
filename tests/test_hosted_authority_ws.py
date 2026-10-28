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


async def test_a_disconnect_is_seen_while_the_turn_is_still_being_prepared(
    served_dir, tmp_path_factory
):
    """Recording and image expansion await inside the turn's dispatch; the
    grant must end when the peer goes, not when that dispatch finishes."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        slow = asyncio.Event()
        reached = []

        async def delayed_submit(blocks, viewer=None):
            reached.append(blocks)
            await slow.wait()  # the backend preparing the prompt

        session.submit_turn = delayed_submit
        held = await _send(app, "alice", "first")
        assert reached and app.agent_bus.hosted_turn_live
        await held.close()
        await asyncio.sleep(0.1)
        assert not app.agent_bus.hosted_turn_live  # ended with the dispatch still pending
        assert not slow.is_set()
        slow.set()
    finally:
        await app.agent_stop()


async def test_a_turn_waiting_for_the_lock_when_its_connection_closes_takes_no_grant(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        async with bus.turn_submission_lock:
            held = await _send(app, "bob", "never")  # its dispatch now waits on the lock
            assert not bus.hosted_turn_live
            await held.close()  # revoked before it ever held a grant: release has nothing to end
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        assert not bus.hosted_turn_live and bus.hosted_turn_principal is None
        assert _logged(app, "user_turn") == []
        assert session.submitted_turns == []
    finally:
        await app.agent_stop()


async def test_an_interrupt_cancels_a_turn_still_waiting_for_the_lock(served_dir, tmp_path_factory):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        async with bus.turn_submission_lock:
            held = _Held(app, "alice")
            await held.open([HELLO, {**_turn("never"), "client_id": "c-1"}, {"type": "interrupt"}])
            assert not bus.hosted_turn_live
        await asyncio.sleep(0.3)
        assert not bus.hosted_turn_live and bus.hosted_turn_principal is None
        assert _logged(app, "user_turn") == []
        assert session.submitted_turns == []
        await held.close()
    finally:
        await app.agent_stop()


async def test_another_principals_interrupt_also_cancels_a_turn_waiting_for_the_lock(
    served_dir, tmp_path_factory
):
    """The submission lock is project-wide, so an interrupt on Bob's connection
    reaches Alice's detached turn waiting on it."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        async with bus.turn_submission_lock:
            alice = await _Held(app, "alice").open([HELLO, {**_turn("never"), "client_id": "c-1"}])
            bob = await _Held(app, "bob").open([HELLO, {"type": "interrupt"}])
        await asyncio.sleep(0.3)
        assert not bus.hosted_turn_live and bus.hosted_turn_principal is None
        assert _logged(app, "user_turn") == []
        assert session.submitted_turns == []
        await alice.close()
        await bob.close()
    finally:
        await app.agent_stop()


async def _closed_worker(hosted):
    """The old agent is gone (closed by a cancel, then the 'cancelled' mark
    lifted as an idle close would leave it): the next interaction would open
    a successor."""
    await hosted.call("post", "/agent/session/cancel", _admin())
    hosted.app.agent_holder._cancelled = False


def _admin():
    return {**_as("alice"), "Content-Type": "application/json"}


async def test_a_fenced_worker_opens_no_successor_by_a_turn_or_a_launch(
    served_dir, tmp_path_factory
):
    """After a kill that could not be verified, neither a hosted turn (ensure) nor
    an HTTP launch may call the session factory."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        await _closed_worker(hosted)
        app.agent_bus.hosted_fenced = True
        held = await _send(app, "alice", "again")  # ensure(retry=True) before admission
        res = await hosted.call("post", "/agent/session", _admin())
        assert len(hosted.sessions) == 1  # the factory was never called again
        assert res.status_code == 503  # not opened: the route reports no session
        assert _logged(app, "user_turn") == []
        await held.close()
    finally:
        await app.agent_stop()


async def test_without_the_fence_the_same_turn_does_open_the_successor(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        await _closed_worker(hosted)
        held = await _send(app, "alice", "again")
        assert len(hosted.sessions) == 2  # control: the fence is what stopped it above
        await held.close()
    finally:
        await app.agent_stop()


async def test_a_manual_interrupt_goes_through_the_bounded_stop_and_the_grant_ends(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        held = await _send(app, "alice", "first")
        assert bus.hosted_turn_live
        interrupter = await _Held(app, "alice").open([HELLO, {"type": "interrupt"}])
        await asyncio.sleep(0.2)
        assert session.interrupted == 1  # once, by the stop lifecycle
        assert not bus.hosted_turn_live and bus.hosted_stopping == 0
        await interrupter.close()
        await held.close()
    finally:
        await app.agent_stop()


async def test_an_agent_error_without_a_turn_end_leaves_the_grant_for_disconnect_and_expiry(
    served_dir, tmp_path_factory
):
    """A stream error can leave the session ready and the turn still running:
    the grant must survive it so a revocation or the expiry still stops the turn."""
    from annealage_ember.session.base import AgentError

    for how in ("disconnect", "expiry"):
        verifier = _Verifier(expiry=time.time() + 1.0 if how == "expiry" else None)
        hosted = _build(served_dir, tmp_path_factory, verifier=verifier)
        app = hosted.app
        await app.agent_start()
        try:
            (session,) = hosted.sessions
            bus = app.agent_bus
            held = await _send(app, "alice", "first")
            session.emit(AgentError(stderr="stream failed", remediation="retry"))
            assert bus.hosted_turn_live, how  # not cleared by the error
            if how == "disconnect":
                await held.close()
            else:
                await asyncio.sleep(1.4)
            await asyncio.sleep(0.2)
            assert session.interrupted == 1, how  # the stop lifecycle still owns the turn
            assert not bus.hosted_turn_live
            if how == "expiry":
                await held.close()
        finally:
            await app.agent_stop()


async def test_no_successor_while_a_stop_is_pending_even_if_the_old_session_already_looks_down(
    served_dir, tmp_path_factory
):
    """The stop marks the old session unavailable before it knows whether the
    process is gone; a concurrent ensure/launch must not replace it then."""
    from annealage_ember.session.base import AgentStatus

    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    gate = asyncio.Event()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus

        async def slow_failing_stop():
            session.set_status("unavailable")
            session.emit(AgentStatus(status="unavailable"))  # looks down to the holder
            await gate.wait()
            return False  # and the kill could not be verified

        session.stop_for_revocation = slow_failing_stop
        held = await _send(app, "alice", "first")
        await held.close()  # revocation: the stop starts and is pending
        await asyncio.sleep(0.1)
        assert bus.hosted_stopping == 1 and app.agent_holder._down
        # Concurrent human interactions while the stop is pending.
        again = await _send(app, "bob", "again")
        res = await hosted.call("post", "/agent/session", _admin())
        assert len(hosted.sessions) == 1 and res.status_code in (200, 503)
        assert json.loads(res.body).get("launched") is not True  # nothing was launched
        gate.set()
        await asyncio.sleep(0.2)
        assert bus.hosted_fenced is True
        await _send(app, "bob", "and again")
        assert len(hosted.sessions) == 1  # fenced for good
        await again.close()
    finally:
        gate.set()
        await app.agent_stop()
