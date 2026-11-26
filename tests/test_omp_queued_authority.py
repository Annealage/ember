"""Queued OMP inputs keep the authority under which they were admitted."""

import asyncio
import contextvars
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("omp_rpc")

from test_omp_session import _started_session, _text  # noqa: E402

from annealage_ember import hosted_authority  # noqa: E402
from annealage_ember.app import AgentHolder  # noqa: E402
from annealage_ember.session.base import InputState  # noqa: E402
from annealage_ember.session.rpc_client import SendRefused  # noqa: E402

pytestmark = pytest.mark.asyncio


def _bus():
    return SimpleNamespace(
        hosted_stopping=0,
        hosted_turn_live=False,
        hosted_turn_secret=None,
        hosted_secret_history=[],
        hosted_fenced=False,
        hosted_lock=threading.Lock(),
        hosted_turn_generation=0,
        hosted_cancel_generation=0,
        hosted_quiescing=False,
        hosted_worker=("alice", "w", "p", "j", ("project.exec",)),
        hosted_mode=True,
    )


def _admit(bus, principal="alice"):
    assert principal == "alice"
    assert hosted_authority.admit_internal(bus, None) is None


async def _block_barrier(session, fake, kind):
    entered, release = threading.Event(), threading.Event()
    if kind == "profile":
        original = fake.set_model

        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("profile test barrier timed out")
            return original(*args)

        fake.set_model = blocked
        session.queue_model_profile("example/model", "low")
        task = session._profile_task
    else:
        fake.context_usage = SimpleNamespace(tokens=150000, context_window=200000, percent=75.0)
        session._idle_compact_tokens = 100000
        original = fake.get_state

        def blocked():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("compaction test barrier timed out")
            return original()

        fake.get_state = blocked
        session._context_tokens = fake.context_usage.tokens
        session._idle_compaction_due()
        task = session._compact_task
    assert task is not None and await asyncio.to_thread(entered.wait, 2)
    return task, release


@pytest.mark.parametrize("kind", ("profile", "compaction"))
@pytest.mark.parametrize("grant", ("valid", "interrupted", "replaced", "standalone"))
async def test_queued_input_keeps_its_admitted_grant(kind, grant):
    token = hosted_authority.GENERATION.set(None)
    session, fake, recorder, _ = await _started_session(base_url=None)
    release = None
    try:
        bus = _bus()
        if grant != "standalone":
            session.send_guard = lambda: hosted_authority.checker(bus)
        task, release = await _block_barrier(session, fake, kind)
        if grant != "standalone":
            _admit(bus)
        await session.submit_turn(_text("queued"))
        assert [e.state for e in recorder.all if isinstance(e, InputState)] == [
            "received",
            "queued",
        ]
        if grant in ("interrupted", "replaced"):
            hosted_authority.interrupt(bus, None)
        if grant == "replaced":
            _admit(bus)
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 5)
        if grant in ("valid", "standalone"):
            assert [call.message for call in fake.prompt_calls] == ["queued"]
            assert any(e.state == "provider_ack" for e in recorder.all if isinstance(e, InputState))
        else:
            assert fake.prompt_calls == []
            assert any(
                e.state == "rejected" and "access to this project ended" in e.detail
                for e in recorder.all
                if isinstance(e, InputState)
            )
    finally:
        if release is not None:
            release.set()
        await session.close()
        hosted_authority.GENERATION.reset(token)


async def test_queued_grant_is_rechecked_at_the_write_gate():
    token = hosted_authority.GENERATION.set(None)
    session, fake, recorder, _ = await _started_session(base_url=None)
    release = None
    write_ready, write_release = threading.Event(), threading.Event()
    try:
        bus = _bus()
        session.send_guard = lambda: hosted_authority.checker(bus)
        task, release = await _block_barrier(session, fake, "profile")
        _admit(bus)
        await session.submit_turn(_text("queued"))

        @contextmanager
        def send_gate(check):
            fake.check = check
            yield

        def prompt(*args, **kwargs):
            write_ready.set()
            if not write_release.wait(5):
                raise RuntimeError("write gate test timed out")
            reason = fake.check()
            if reason:
                raise SendRefused(reason)
            fake.prompt_calls.append(args)

        fake.send_gate = send_gate
        fake.prompt = prompt
        release.set()
        assert await asyncio.to_thread(write_ready.wait, 2)
        hosted_authority.interrupt(bus, None)
        write_release.set()
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert fake.prompt_calls == []
        assert any(e.state == "rejected" for e in recorder.all if isinstance(e, InputState))
    finally:
        if release is not None:
            release.set()
        write_release.set()
        await session.close()
        hosted_authority.GENERATION.reset(token)


async def test_deferred_start_preserves_grant_through_omp_profile_queue():
    token = hosted_authority.GENERATION.set(None)
    session, fake, recorder, _ = await _started_session(base_url=None)
    release = None
    try:
        bus = _bus()
        session.send_guard = lambda: hosted_authority.checker(bus)
        task, release = await _block_barrier(session, fake, "profile")
        start = asyncio.get_running_loop().create_future()
        holder = SimpleNamespace(
            session=session, bus=bus, _start_task=start, _deferred=[], _publish=recorder
        )
        _admit(bus)
        holder._deferred.append(
            (session, _text("deferred"), None, 1, "alice", bus.hosted_turn_generation)
        )
        drain = contextvars.Context().run(asyncio.create_task, AgentHolder._drain_deferred(holder))
        start.set_result(None)
        await drain
        release.set()
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert [call.message for call in fake.prompt_calls] == ["deferred"]
    finally:
        if release is not None:
            release.set()
        await session.close()
        hosted_authority.GENERATION.reset(token)
