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
from annealage_ember.session.base import TurnEnd, UserTurn

pytestmark = pytest.mark.asyncio


class _Verifier:
    """Two principals, and a delegation that outlives the test or does not."""

    def __init__(self, expiry=None, ops=("project.read", "project.exec")):
        self.expiry = expiry
        self.ops = ops

    def verify(self, token, operation):
        subs = {"alice": PRINCIPAL, "bob": OTHER}
        if token not in subs:
            raise ValueError("invalid delegation")
        return SimpleNamespace(
            sub=subs[token],
            wsp="wsp_test",
            prj="prj_test",
            ops=self.ops,
            job="job_test",
            rev="a" * 40,
            exp=self.expiry if self.expiry is not None else time.time() + 60,
        )


def _as(who):
    return {"Authorization": "Bearer " + who, "X-Annealage-Principal-Display": who}


def _decode(data):
    """The JSON frames in a server's (unmasked) websocket text frames."""
    frames, i = [], 0
    while i < len(data):
        opcode, length = data[i] & 0x0F, data[i + 1] & 0x7F
        i += 2
        if length == 126:
            length, i = int.from_bytes(data[i : i + 2], "big"), i + 2
        elif length == 127:
            length, i = int.from_bytes(data[i : i + 8], "big"), i + 8
        payload, i = data[i : i + length], i + length
        if opcode == 1:
            frames.append(json.loads(payload))
    return frames


class _Held:
    """One open ``/ws`` connection that stays open until ``close()``: the
    server only sees the peer go away then, as it does when the front closes it."""

    def __init__(self, app, who):
        self.app, self.who = app, who
        self.gone = asyncio.Event()
        self.task = None
        self.sock = None

    def frames(self):
        """Every frame the server has written on this connection so far."""
        data = b"".join(self.sock.written)
        return _decode(data[data.index(b"\r\n\r\n") + 4 :])

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

        sock = self.sock = Sock(request + inbound)
        req = await Request.create(self.app, sock, sock, ("127.0.0.1", 1234), scheme=None)
        self.task = asyncio.ensure_future(self.app.dispatch_request(req))
        await asyncio.sleep(0.2)
        return self

    async def close(self):
        self.gone.set()
        await asyncio.wait_for(self.task, 5)


def _hold_the_start(app):
    """The holder's start is still running: an input is deferred until it settles."""
    starting = asyncio.get_running_loop().create_future()
    app.agent_holder._start_task = starting
    return starting


async def _send(app, who, text, client_id="c-1"):
    """A turn sent on a connection that is still open afterwards."""
    held = _Held(app, who)
    return await held.open([HELLO, {**_turn(text), "client_id": client_id}])


def _logged(app, kind):
    return [w for _s, w in app.agent_event_log.replay(0).events if w["kind"] == kind]


async def test_hello_compaction_snapshot_outlives_history_page_and_clears_on_reset(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier(ops=("project.read",)))
    app = hosted.app
    await app.agent_start()
    try:
        log = app.agent_event_log
        event = {
            "kind": "compaction",
            "state": "completed",
            "reason": "auto",
            "context_tokens": 123,
            "at": 10,
        }
        seq = log.append(SimpleNamespace(to_wire=lambda: event))
        for number in range(1, 25):
            log.append(UserTurn(turn=number, blocks=[{"type": "text", "text": str(number)}]))
            log.append(TurnEnd(turn=number, stop_reason="end", cost_usd=0.0))
        held = await _Held(app, "alice").open([{**HELLO, "history_turns": 2}])
        frames = held.frames()
        greeting = next(frame for frame in frames if frame["type"] == "hello")
        page = next(frame for frame in frames if frame["type"] == "history_page")
        assert greeting["compaction"] == {"seq": seq, "event": event}
        assert seq < page["before"]
        assert not any(item["seq"] == seq for item in page["events"])
        await held.close()

        reset = {"kind": "session_reset"}
        log.append(SimpleNamespace(to_wire=lambda: reset))
        held = await _Held(app, "alice").open([{**HELLO, "history_turns": 2}])
        greeting = next(frame for frame in held.frames() if frame["type"] == "hello")
        assert greeting["compaction"] is None
        await held.close()
    finally:
        await app.agent_stop()


async def test_host_without_worker_grant_accepts_only_delegated_turns(served_dir, tmp_path_factory):
    from annealage_ember import hosted_authority

    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier(), hosted_worker_claims=None)
    app = hosted.app
    await app.agent_start()
    try:
        assert hosted_authority.admit_internal(app.agent_bus, app.agent_holder) is not None
        held = await _send(app, "alice", "delegated")
        assert hosted.sessions[0].submitted_turns[0][0][0]["text"] == "delegated"
        assert app.agent_bus.hosted_turn_live
        await held.close()
    finally:
        await app.agent_stop()


async def test_a_browser_turn_cannot_expand_the_worker_launch_grant(served_dir, tmp_path_factory):
    claims = SimpleNamespace(
        sub=PRINCIPAL,
        wsp="wsp_test",
        prj="prj_test",
        job="job_test",
        ops=("project.read", "project.exec"),
        exp=None,
    )
    hosted = _build(
        served_dir,
        tmp_path_factory,
        verifier=_Verifier(ops=("project.read", "project.exec", "project.write")),
        hosted_worker_claims=claims,
    )
    app = hosted.app
    await app.agent_start()
    try:
        held = await _send(app, "alice", "run")
        assert hosted.sessions[0].submitted_turns
        assert app.agent_bus.hosted_turn_ops == ("project.read", "project.exec")
        await held.close()
    finally:
        await app.agent_stop()


@pytest.mark.parametrize("failure", ["authority", "durable_fence"])
async def test_failed_outbox_admission_releases_hosted_turn(
    served_dir, tmp_path_factory, monkeypatch, failure
):
    from annealage_ember.identity import Human
    from annealage_ember.review.notifications import NotificationError
    from annealage_ember.review.outbox import OrdinaryTurnOutbox

    verifier = _Verifier()
    hosted = _build(served_dir, tmp_path_factory, verifier=verifier)
    app = hosted.app
    await app.agent_start()
    try:
        outbox = OrdinaryTurnOutbox(
            app,
            path=served_dir / "ordinary-turns.json",
            scope="project",
            auth=app.agent_auth,
            recorder=None,
        )
        claims = verifier.verify("alice", "project.exec")
        human = Human(
            principal_id=PRINCIPAL,
            workspace_id=claims.wsp,
            project_id=claims.prj,
            hosted_claims=claims,
        )
        blocks = [{"type": "text", "text": "queued"}]
        outbox.enqueue("op", human, blocks)
        if failure == "authority":
            permits = app.agent_auth.permits
            calls = []

            def expire_on_recheck(actor, operation):
                calls.append(operation)
                return len(calls) == 1 and permits(actor, operation)

            monkeypatch.setattr(app.agent_auth, "permits", expire_on_recheck)
            expected = NotificationError
        else:
            update = outbox._update

            def fail_fence(operation_id, **changes):
                if changes.get("status") == "dispatching":
                    raise OSError("fence write failed")
                return update(operation_id, **changes)

            monkeypatch.setattr(outbox, "_update", fail_fence)
            expected = OSError

        with pytest.raises(expected):
            await outbox.deliver()
        assert not app.agent_bus.hosted_turn_live
        assert app.agent_bus.hosted_turn_ops == ()
        assert hosted.sessions[0].submitted_turns == []
        assert _logged(app, "user_turn") == []
        assert outbox.status("op")["status"] == "queued"

        monkeypatch.undo()
        await outbox.deliver()
        assert outbox.status("op")["status"] == "sent"
        assert hosted.sessions[0].submitted_turns[0][0] == blocks
        assert app.agent_bus.hosted_turn_live
    finally:
        await app.agent_stop()


async def test_verified_collaborator_handover_keeps_actor_and_cannot_expand_worker(
    served_dir, tmp_path_factory
):
    from annealage_ember.identity import Human
    from annealage_ember.review.outbox import OrdinaryTurnOutbox

    verifier = _Verifier(ops=("project.read", "project.exec", "project.write"))
    hosted = _build(served_dir, tmp_path_factory, verifier=verifier)
    app = hosted.app
    await app.agent_start()
    try:
        claims = verifier.verify("bob", "project.exec")
        human = Human(
            principal_id=OTHER, workspace_id=claims.wsp, project_id=claims.prj, hosted_claims=claims
        )
        blocks = [{"type": "text", "text": "handover"}]
        outbox = OrdinaryTurnOutbox(
            app,
            path=served_dir / "handover.json",
            scope="project",
            auth=app.agent_auth,
            recorder=None,
            require_verified_human=True,
        )
        outbox.enqueue("op", human, blocks)
        await outbox.deliver()  # the background pump cannot use persisted claims
        await outbox.deliver(
            verified_human=human,
            expected_operation="op",
            expected_blocks=[{"type": "text", "text": "forged"}],
        )
        assert not hosted.sessions[0].submitted_turns
        await outbox.deliver(verified_human=human, expected_operation="op", expected_blocks=blocks)
        assert hosted.sessions[0].submitted_turns[0][0] == blocks
        assert app.agent_bus.hosted_turn_ops == ("project.read", "project.exec")
        assert _logged(app, "user_turn")[-1]["principal_id"] == OTHER
    finally:
        await app.agent_stop()


async def test_another_principals_message_steers_the_running_turn_without_new_authority(
    served_dir, tmp_path_factory
):
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
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["first", "second"]
        assert (bus.hosted_turn_principal, bus.hosted_turn_ops, bus.hosted_turn_secret) == before
        assert [w["client_id"] for w in _logged(app, "user_turn")] == ["c-1", "c-2"]
    finally:
        await app.agent_stop()


async def test_a_message_steers_a_running_turn_during_an_automatic_continuation(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        await _send(app, "alice", "first")
        (session,) = hosted.sessions
        app.agent_bus.manual_turn_refusal = lambda: "Automatic continuation is active"
        await _send(app, "bob", "steer", client_id="c-2")
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["first", "steer"]
    finally:
        await app.agent_stop()


async def test_the_owner_can_steer_their_own_running_turn_without_new_authority(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        await _send(app, "alice", "first")
        bus = app.agent_bus
        before = (bus.hosted_turn_generation, bus.hosted_turn_secret, bus.hosted_turn_ops)
        await _send(app, "alice", "steer", client_id="c-2")
        (session,) = hosted.sessions
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["first", "steer"]
        assert (bus.hosted_turn_generation, bus.hosted_turn_secret, bus.hosted_turn_ops) == before
        assert bus.hosted_turn_live
    finally:
        await app.agent_stop()


async def test_a_turn_still_running_when_its_delegation_expires_keeps_running(
    served_dir, tmp_path_factory
):
    """``exp`` gates admission and the socket, not the turn that got in."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier(expiry=time.time() + 0.5))
    app = hosted.app
    await app.agent_start()
    try:
        await _send(app, "alice", "first")
        (session,) = hosted.sessions
        bus = app.agent_bus
        assert session.interrupted == 0 and bus.hosted_turn_live
        await asyncio.sleep(0.8)
        assert session.interrupted == 0 and bus.hosted_turn_live
        assert bus.hosted_turn_ops and bus.hosted_turn_secret is not None
        # Only its own end ends the grant.
        session.emit(TurnEnd(turn=1, stop_reason="end_turn", cost_usd=None))
        assert not bus.hosted_turn_live
    finally:
        await app.agent_stop()


async def test_losing_the_connection_that_started_a_turn_changes_nothing_about_it(
    served_dir, tmp_path_factory
):
    """A browser is a view that comes and goes: the front closing the socket (a
    closed tab, a dropped network, an expired delegation) does not stop the turn."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        held = await _send(app, "alice", "first")
        (session,) = hosted.sessions
        bus = app.agent_bus
        secret = bus.hosted_turn_secret
        assert bus.hosted_turn_live and session.interrupted == 0
        await held.close()
        await asyncio.sleep(0.2)
        assert bus.hosted_turn_live and bus.hosted_turn_secret == secret
        assert session.interrupted == 0 and bus.hosted_stopping == 0
    finally:
        await app.agent_stop()


async def test_a_prompt_waiting_for_the_agent_to_start_is_still_dispatched_when_its_tab_closes(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        session.set_status("connecting")
        starting = _hold_the_start(app)
        held = await _send(app, "alice", "wait for me")
        assert session.submitted_turns == []
        assert any(w["state"] == "queued" for w in _logged(app, "input_state"))
        await held.close()  # the tab goes while the agent is still starting
        await asyncio.sleep(0.1)
        session.set_status("ready")
        starting.set_result(None)
        await asyncio.sleep(0.5)
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["wait for me"]
    finally:
        await app.agent_stop()


async def test_a_prompt_waiting_for_the_agent_to_start_is_not_dispatched_after_an_interrupt(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        session.set_status("connecting")
        starting = _hold_the_start(app)
        held = await _send(app, "alice", "wait for me")
        assert session.submitted_turns == []
        interrupter = await _Held(app, "alice").open([HELLO, {"type": "interrupt"}])
        await asyncio.sleep(0.1)
        session.set_status("ready")
        starting.set_result(None)
        await asyncio.sleep(0.5)
        assert session.submitted_turns == []
        assert [w["stop_reason"] for w in _logged(app, "turn_end")] == ["rejected"]
        await interrupter.close()
        await held.close()
    finally:
        await app.agent_stop()


async def test_a_disconnect_while_the_turn_is_being_prepared_does_not_stop_it(
    served_dir, tmp_path_factory
):
    """Recording and image expansion await inside the turn's dispatch; the peer
    going away in that time changes nothing: the turn is still sent."""
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        slow = asyncio.Event()
        reached, sent = [], []

        async def delayed_submit(blocks, viewer=None):
            reached.append(blocks)
            await slow.wait()  # the backend preparing the prompt
            sent.append(blocks)

        session.submit_turn = delayed_submit
        held = await _send(app, "alice", "first")
        assert reached and app.agent_bus.hosted_turn_live
        await held.close()
        await asyncio.sleep(0.1)
        assert app.agent_bus.hosted_turn_live and not sent
        slow.set()
        await asyncio.sleep(0.1)
        assert sent and app.agent_bus.hosted_turn_live
    finally:
        await app.agent_stop()


async def test_a_turn_waiting_for_the_lock_when_its_tab_closes_still_runs(
    served_dir, tmp_path_factory
):
    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        async with bus.turn_submission_lock:
            held = await _send(app, "alice", "still sent")  # dispatch waits on the lock
            assert not bus.hosted_turn_live
            await held.close()
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.3)
        assert bus.hosted_turn_live and bus.hosted_turn_principal == PRINCIPAL
        assert [w["client_id"] for w in _logged(app, "user_turn")] == ["c-1"]
        assert [b[0]["text"] for b, _v in session.submitted_turns] == ["still sent"]
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


async def test_an_agent_error_without_a_turn_end_leaves_the_grant_in_place(
    served_dir, tmp_path_factory
):
    """A stream error can leave the session ready and the turn still running:
    the grant must survive it, so an explicit interrupt still stops the turn."""
    from annealage_ember.session.base import AgentError

    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        held = await _send(app, "alice", "first")
        session.emit(AgentError(stderr="stream failed", remediation="retry"))
        assert bus.hosted_turn_live  # not cleared by the error
        await held.close()
        await asyncio.sleep(0.2)
        assert session.interrupted == 0 and bus.hosted_turn_live
        interrupter = await _Held(app, "alice").open([HELLO, {"type": "interrupt"}])
        await asyncio.sleep(0.2)
        assert session.interrupted == 1  # the stop lifecycle still owns the turn
        assert not bus.hosted_turn_live
        await interrupter.close()
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
        interrupter = await _Held(app, "alice").open([HELLO, {"type": "interrupt"}])
        await asyncio.sleep(0.1)  # the stop starts and is pending
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
        await interrupter.close()
        await held.close()
    finally:
        gate.set()
        await app.agent_stop()


async def test_a_viewer_attaching_mid_turn_after_another_left_gets_the_replay_and_can_act(
    served_dir, tmp_path_factory
):
    """The browser is a view that comes and goes: a second one, under its own
    claims, is caught up from its cursor (events since, the open card) and can
    answer the card and interrupt the turn the first one started."""
    from annealage_ember.session.base import PermissionRequest, TextDelta

    verifier = _Verifier(ops=("project.read", "project.exec", "project.write"))
    hosted = _build(served_dir, tmp_path_factory, verifier=verifier)
    app = hosted.app
    await app.agent_start()
    try:
        (session,) = hosted.sessions
        bus = app.agent_bus
        first = await _send(app, "alice", "review this")
        hello = [f for f in first.frames() if f["type"] == "hello"][0]
        cursor = hello["seq"]
        session.emit(TextDelta(turn=1, text="reading"))
        session.emit(PermissionRequest(request_id="pr_held", tool="edit", input={}))
        await asyncio.sleep(0.1)
        seen = [f for f in first.frames() if f["type"] == "event"]
        assert [f["event"]["kind"] for f in seen if f["seq"] > cursor][-2:] == [
            "text_delta",
            "permission_request",
        ]
        await first.close()
        await asyncio.sleep(0.1)
        assert app.agent_holder.status()["viewers"] == 0
        assert bus.hosted_turn_live and session.interrupted == 0

        # More happens with nobody attached.
        session.emit(TextDelta(turn=1, text=" more"))

        second = await _Held(app, "bob").open([{**HELLO, "last_seq": cursor}])
        replay = [
            f
            for f in second.frames()
            if f["type"] == "event" and f["event"]["kind"] != "viewer_primary"
        ]
        assert [f["event"]["kind"] for f in replay] == [
            "user_turn",
            "text_delta",
            "permission_request",
            "text_delta",
        ]
        assert [f["seq"] for f in replay] == sorted(f["seq"] for f in replay)
        assert replay[-1]["event"]["text"] == " more"
        assert app.agent_holder.status()["viewers"] == 1
        await second.close()

        # Under its own claims: bob answers the card and interrupts.
        third = await _Held(app, "bob").open(
            [
                HELLO,
                {"type": "permission", "request_id": "pr_held", "decision": "allow"},
                {"type": "interrupt"},
            ]
        )
        await asyncio.sleep(0.3)
        assert session.permission_decisions == [("pr_held", "allow", "")]
        assert session.interrupted == 1 and not bus.hosted_turn_live
        await third.close()
    finally:
        await app.agent_stop()


async def test_worker_authority_survives_turn_and_disconnect_for_internal_work(
    served_dir, tmp_path_factory
):
    from annealage_ember import hosted_authority

    hosted = _build(served_dir, tmp_path_factory, verifier=_Verifier())
    app = hosted.app
    await app.agent_start()
    try:
        held = await _send(app, "alice", "first")
        session = hosted.sessions[0]
        bus = app.agent_bus
        secret = bus.hosted_turn_secret
        await held.close()
        session.emit(TurnEnd(turn=bus.turn, stop_reason="end_turn", cost_usd=None))
        assert not bus.hosted_turn_live and bus.hosted_turn_secret is None
        assert hosted_authority.admit_internal(bus, app.agent_holder) is None
        assert bus.hosted_turn_secret and bus.hosted_turn_secret != secret
        await session.submit_turn([{"type": "text", "text": "internal"}])
        assert [blocks[0]["text"] for blocks, _ in session.submitted_turns] == ["first", "internal"]
    finally:
        await app.agent_stop()
