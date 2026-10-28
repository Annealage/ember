"""The provider-send boundary: a hosted prompt is checked at the send, after the awaits that can outlast its grant."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from test_omp_session import _started_session

from annealage_ember import hosted_authority
from annealage_ember.session import omp as omp_module
from annealage_ember.session.base import InputState

pytestmark = pytest.mark.asyncio


def _bus(exp):
    return SimpleNamespace(
        hosted_turn_live=True,
        hosted_turn_ops=("project.exec",),
        hosted_turn_exp=exp,
        hosted_turn_secret="s",
        hosted_turn_principal="alice",
        hosted_turn_owner="conn",
        hosted_turn_generation=1,
        hosted_lock=threading.Lock(),
        hosted_expiry_handle=None,
    )


async def _submit_during_slow_expansion(monkeypatch, bus, *, while_expanding):
    """Submit one turn whose image expansion is held; run ``while_expanding``
    (the grant ending), then let it finish. Returns the fake client."""
    release = threading.Event()
    started = threading.Event()

    def slow_expand(blocks, cwd):
        started.set()
        release.wait(5)
        return blocks

    monkeypatch.setattr(omp_module.turn_images, "expand_turn_blocks", slow_expand)
    session, fake, recorder, _broker = await _started_session()
    session.send_guard = lambda: hosted_authority.checker(bus)
    hosted_authority.GENERATION.set(bus.hosted_turn_generation)
    try:
        task = asyncio.ensure_future(session.submit_turn([{"type": "text", "text": "go"}]))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        while_expanding(session)
        release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.05)
        return fake, recorder
    finally:
        release.set()
        await session.close()


async def test_a_prompt_is_sent_while_the_grant_still_holds(monkeypatch):
    fake, _ = await _submit_during_slow_expansion(
        monkeypatch, _bus(time.time() + 60), while_expanding=lambda s: None
    )
    assert [c.message for c in fake.prompt_calls] == ["go"]


async def test_a_grant_released_during_image_expansion_is_not_sent(monkeypatch):
    bus = _bus(time.time() + 60)
    fake, recorder = await _submit_during_slow_expansion(
        monkeypatch,
        bus,
        while_expanding=lambda s: hosted_authority.release(bus, "conn", None),
    )
    assert fake.prompt_calls == []
    rejected = [e for e in recorder.all if isinstance(e, InputState) and e.state == "rejected"]
    assert rejected and "access" in rejected[0].detail


async def test_a_grant_that_expires_during_image_expansion_is_not_sent(monkeypatch):
    bus = _bus(time.time() + 0.3)
    fake, _ = await _submit_during_slow_expansion(
        monkeypatch, bus, while_expanding=lambda s: time.sleep(0.4)
    )
    assert fake.prompt_calls == []


async def test_a_prompt_queued_behind_a_busy_executor_is_not_sent_after_revocation(monkeypatch):
    bus = _bus(time.time() + 60)
    session, fake, recorder, _broker = await _started_session()
    session.send_guard = lambda: hosted_authority.checker(bus)
    hosted_authority.GENERATION.set(bus.hosted_turn_generation)
    unblock = threading.Event()
    try:
        session._executor.submit(unblock.wait, 5)  # the executor is saturated
        task = asyncio.ensure_future(session.submit_turn([{"type": "text", "text": "go"}]))
        await asyncio.sleep(0.2)  # past the early check, queued behind the busy call
        assert fake.prompt_calls == []
        hosted_authority.release(bus, "conn", None)
        unblock.set()
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.05)
        assert fake.prompt_calls == []
    finally:
        unblock.set()
        await session.close()


async def test_a_prompt_is_never_sent_under_a_newer_grant_than_the_one_it_was_admitted_under(
    monkeypatch,
):
    bus = _bus(time.time() + 60)
    session, fake, _recorder, _broker = await _started_session()
    session.send_guard = lambda: hosted_authority.checker(bus)
    hosted_authority.GENERATION.set(bus.hosted_turn_generation)
    try:
        hosted_authority.release(bus, "conn", None)
        # Someone else is admitted meanwhile: a live grant, but not this turn's.
        assert (
            hosted_authority.admit(
                bus, None, SimpleNamespace(sub="bob", ops=(), exp=time.time() + 60)
            )
            is None
        )
        hosted_authority.GENERATION.set(1)
        task = asyncio.ensure_future(session.submit_turn([{"type": "text", "text": "go"}]))
        await asyncio.wait_for(task, 5)
        assert fake.prompt_calls == []
    finally:
        await session.close()


async def test_the_send_check_never_sees_a_half_written_grant():
    """The check takes the lock admit/release take, so it cannot interleave
    with them (it does not, and cannot, order the provider call after it)."""
    bus = _bus(time.time() + 60)
    hosted_authority.GENERATION.set(bus.hosted_turn_generation)
    check = hosted_authority.checker(bus)
    done = threading.Event()
    with bus.hosted_lock:  # a release (or admit) in progress
        thread = threading.Thread(target=lambda: (check(), done.set()))
        thread.start()
        assert not done.wait(0.2)
    thread.join(2)
    assert done.is_set()


# -- a hosted prompt accepted while the omp agent is still starting ---------------


def _delayed_start_app(served_dir, tmp_path_factory, verifier, gate, built):
    from conftest import create_toy_app
    from test_hosted_session import _Sink
    from test_omp_session import FakeRpcClient, _tool_table

    from annealage_ember import sessions, settings
    from annealage_ember.session.omp import OmpSession
    from annealage_ember.session.permissions import PermissionBroker

    def build_session(on_event, *, bus):
        class SlowToStart(FakeRpcClient):
            def get_state(self):
                gate.wait(10)  # the agent coming up, in the executor thread
                return super().get_state()

        def client_factory(**kwargs):
            fake = SlowToStart(**kwargs)
            built.append(fake)
            return fake

        return OmpSession(
            on_event,
            cwd=str(served_dir),
            session_id="hosted-session",
            broker=PermissionBroker(on_event, timeout=2.0, no_viewer_grace=0.05),
            tool_table=_tool_table(),
            client_factory=client_factory,
            base_url="http://127.0.0.1:11434/v1",
        )

    state_dir = tmp_path_factory.mktemp("worker-state")
    sessions.create_session(state_dir, "hosted-session")
    return create_toy_app(
        served_dir,
        session_id="hosted-session",
        build_session=build_session,
        hosted_mode=True,
        hosted_verifier=verifier,
        hosted_state_dir=state_dir,
        hosted_tool_ops={"mcp__toy__list_notes": "project.read"},
        settings=settings.resolve(tmp_path_factory.mktemp("service"), flags={"backend": "omp"}),
        state_sink=_Sink(),
        start_closed=True,
        idle_timeout=None,
    )


async def test_a_hosted_prompt_accepted_while_omp_starts_is_delivered_with_its_own_grant(
    served_dir, tmp_path_factory
):
    from test_hosted_authority_ws import _send, _Verifier

    gate, built = threading.Event(), []
    app = _delayed_start_app(served_dir, tmp_path_factory, _Verifier(), gate, built)
    await app.agent_start()
    try:
        held = await _send(app, "alice", "after the start")
        assert not any(c.prompt_calls for c in built)  # still starting; the prompt is deferred
        gate.set()
        for _ in range(100):
            if built and built[0].prompt_calls:
                break
            await asyncio.sleep(0.05)
        assert [c.message for c in built[0].prompt_calls] == ["after the start"]
        await held.close()
    finally:
        gate.set()
        await app.agent_stop()


async def test_a_hosted_prompt_waiting_for_omp_to_start_is_refused_once_revoked_or_expired(
    served_dir, tmp_path_factory
):
    from test_hosted_authority_ws import _send, _Verifier

    for how in ("revoked", "expired"):
        gate, built = threading.Event(), []
        verifier = _Verifier(expiry=time.time() + 1.5 if how == "expired" else None)
        app = _delayed_start_app(served_dir, tmp_path_factory, verifier, gate, built)
        await app.agent_start()
        try:
            held = await _send(app, "alice", "too late")
            if how == "revoked":
                await held.close()
            else:
                await asyncio.sleep(1.8)
            gate.set()
            await asyncio.sleep(1.0)
            assert not built or built[0].prompt_calls == [], how
            if how == "expired":
                await held.close()
        finally:
            gate.set()
            await app.agent_stop()


async def test_interrupt_is_not_queued_behind_a_prompt_that_is_waiting_for_omp(monkeypatch):
    """Abort has its own worker: the prompt call holds the session's executor
    while it waits for omp's response, and an abort queued there would wait too."""
    session, fake, _recorder, _broker = await _started_session()
    unblock = threading.Event()
    aborted = threading.Event()
    original = fake.prompt

    def slow_prompt(*args, **kwargs):
        unblock.wait(5)
        return original(*args, **kwargs)

    fake.prompt = slow_prompt
    fake.abort = lambda: aborted.set()
    try:
        task = asyncio.ensure_future(session.submit_turn([{"type": "text", "text": "go"}]))
        await asyncio.sleep(0.2)  # the prompt is now holding the executor
        await asyncio.wait_for(session.interrupt(), 2)
        assert aborted.is_set()
        unblock.set()
        await asyncio.wait_for(task, 5)
    finally:
        unblock.set()
        await session.close()


async def test_a_revocation_stop_that_cannot_get_an_abort_through_kills_omp_and_fails_closed():
    from annealage_ember.session.base import AGENT_UNAVAILABLE

    session, fake, _recorder, _broker = await _started_session()
    stuck, killed = threading.Event(), []
    fake.abort = lambda: stuck.wait(5)  # an abort queued behind a stalled write
    fake.kill = lambda: killed.append(1)
    try:
        started = time.monotonic()
        await session.stop_for_revocation(deadline=0.3)
        assert killed == [1] and time.monotonic() - started < 2
        assert session.agent_status() == AGENT_UNAVAILABLE
    finally:
        stuck.set()
        await session.close()


async def test_a_revocation_stop_that_gets_its_abort_through_does_not_kill_omp():
    session, fake, _recorder, _broker = await _started_session()
    killed = []
    fake.kill = lambda: killed.append(1)
    try:
        await session.stop_for_revocation(deadline=1.0)
        assert fake.abort_calls == 1 and killed == []
        assert session.agent_status() == "ready"
    finally:
        await session.close()
