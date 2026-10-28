"""The hosted turn authority through a real ``/ws`` and a real app over a stand-in backend."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from test_hosted_session import OTHER, PRINCIPAL, _build
from test_identity import HELLO, _converse, _turn

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


async def _send(app, who, text, client_id="c-1"):
    frame = {**_turn(text), "client_id": client_id}
    res, _ = await _converse(app, [HELLO, frame], headers=_as(who))
    return res


def _logged(app, kind):
    return [w for _s, w in app.agent_event_log.replay(0).events if w["kind"] == kind]


def _refusals(app):
    # A refusal is sent to the sender only; read it from what the server wrote.
    return app


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
