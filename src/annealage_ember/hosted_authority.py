"""Job-scoped authority and per-submission fencing for a hosted worker.

Only a trusted launch grant may install worker authority. Browser delegations
are independently checked at admission and cannot enlarge that authority.
Launch grant expiry, when present, and browser expiry gate admission, not work.
Cancellation cannot undo an external call already started; the bounded stop
must establish that the old backend is gone before another turn can start.
"""

from __future__ import annotations

import asyncio
import contextvars
import secrets
import time
from typing import Optional

from .refusal import Busy

#: The generation of the grant the running task was admitted under. A grant's
#: number is its own: a send stamped with an older one is never made under a
#: newer grant, whoever holds that.
GENERATION = contextvars.ContextVar("hosted_grant_generation", default=None)

# Stamp authenticated bridge requests so a delayed call cannot be adopted by a successor.
BRIDGE_GENERATION = contextvars.ContextVar("hosted_bridge_generation", default=None)

#: ``(bus, bus.hosted_cancel_generation)`` taken by the read loop the moment a
#: turn frame arrives, in the context of the task that will submit it. An
#: interrupt on ANY connection bumps the bus's generation (the submission lock
#: is project-wide, so one principal's interrupt reaches every waiting turn), so
#: a turn still waiting for the lock when that happens takes no grant. The
#: connection going away does not: a turn frame that arrived is still run.
FENCE = contextvars.ContextVar("hosted_turn_fence", default=None)


def fenced_out() -> bool:
    """Whether the turn being submitted was cancelled (interrupt) after its
    frame arrived and before it took the lock."""
    fence = FENCE.get()
    if fence is None:
        return False
    bus, generation = fence
    return bus.hosted_cancel_generation != generation


_IN_FLIGHT = "your previous message is still being worked on, so this one was not sent"


def _in_flight(bus, holder) -> bool:
    if bus.hosted_stopping:
        # A revoked turn is still being stopped: no successor until it is
        # confirmed ended (or torn down), or the late stop could hit it.
        return True
    if bus.hosted_turn_live:
        return True
    return bool(holder is not None and holder.turn_in_flight())


def is_steer(bus) -> bool:
    """Whether a person's message joins the turn already running. A steer takes
    no new authority: the running turn's generation, secret and ops stay as
    they are."""
    return bool(bus.hosted_turn_live and not bus.hosted_stopping)


def install(bus, claims, *, now: Optional[float] = None) -> None:
    """Install the supervisor-revalidated job grant."""
    now = time.time() if now is None else now
    expiry = getattr(claims, "exp", None)
    if (
        claims is None
        or (expiry is not None and now >= expiry)
        or not all(
            isinstance(getattr(claims, key, None), str) and getattr(claims, key)
            for key in ("sub", "wsp", "prj", "job")
        )
    ):
        raise ValueError("a valid job-scoped launch grant is required")
    ops = tuple(claims.ops)
    if "project.exec" not in ops or any(not isinstance(op, str) or not op for op in ops):
        raise ValueError("a launch grant including project.exec is required")
    with bus.hosted_lock:
        if bus.hosted_worker is not None or bus.hosted_fenced:
            raise ValueError("worker authority is already installed or revoked")
        bus.hosted_worker = (claims.sub, claims.wsp, claims.prj, claims.job, ops)
        bus.hosted_secret_history = []


def quiesce(bus) -> None:
    """Close admission while existing work drains for a checkpoint."""
    with bus.hosted_lock:
        bus.hosted_quiescing = True


def enter_frame(bus) -> bool:
    """Count an admitted WebSocket dispatch until its handler settles."""
    with bus.hosted_lock:
        if bus.hosted_quiescing or bus.hosted_fenced:
            return False
        bus.hosted_frames += 1
        return True


def leave_frame(bus) -> None:
    with bus.hosted_lock:
        bus.hosted_frames -= 1


def resume(bus) -> bool:
    """Reopen checkpoint admission only for a still-authorised worker."""
    with bus.hosted_lock:
        if bus.hosted_fenced or bus.hosted_worker is None:
            return False
        bus.hosted_quiescing = False
        return True


def _admit_worker(bus, holder, caller_ops=None, principal=None, steer=False) -> Optional[str]:
    with bus.hosted_lock:
        if bus.hosted_fenced or bus.hosted_worker is None:
            return "this worker has no valid authority to start work"
        if bus.hosted_quiescing:
            return Busy("this worker is being checkpointed")
        if steer and is_steer(bus):
            return None
        if _in_flight(bus, holder):
            return Busy(_IN_FLIGHT)
        bus.hosted_turn_principal = principal or bus.hosted_worker[0]
        bus.hosted_turn_generation += 1
        GENERATION.set(bus.hosted_turn_generation)
        bus.hosted_turn_ops = tuple(
            op for op in bus.hosted_worker[4] if caller_ops is None or op in caller_ops
        )
        bus.hosted_turn_secret = secrets.token_urlsafe(32)
        bus.hosted_secret_history.append(bus.hosted_turn_secret)
        bus.hosted_turn_live = True
    return None


def _admit_claims(bus, holder, claims, *, collaborator, now):
    now = time.time() if now is None else now
    if claims is None or now >= claims.exp:
        return "your access to this project has expired, so this message was not sent"
    worker = bus.hosted_worker
    scoped = (
        worker is not None
        and isinstance(getattr(claims, "sub", None), str)
        and bool(claims.sub)
        and tuple(getattr(claims, key, None) for key in ("wsp", "prj", "job")) == worker[1:4]
        and "project.exec" in claims.ops
        and "project.exec" in worker[4]
    )
    if scoped and is_steer(bus):
        # Joining a running turn takes no authority, so any member of this
        # project may steer it.
        return _admit_worker(bus, holder, claims.ops, claims.sub, steer=True)
    if not scoped or (not collaborator and claims.sub != worker[0]):
        return "your delegation does not authorise this worker's operations"
    return _admit_worker(bus, holder, claims.ops, claims.sub)


def admit(bus, holder, claims, *, now: Optional[float] = None) -> Optional[str]:
    """Admit a browser turn, preserving per-turn authority for other hosts."""
    if getattr(bus, "hosted_worker", None) is not None:
        return _admit_claims(bus, holder, claims, collaborator=False, now=now)
    now = time.time() if now is None else now
    if claims is None or now >= claims.exp:
        return "your access to this project has expired, so this message was not sent"
    with bus.hosted_lock:
        if bus.hosted_fenced:
            return "this worker has no valid authority to start work"
        if getattr(bus, "hosted_quiescing", False):
            return Busy("this worker is being checkpointed")
        if is_steer(bus):
            return None
        if _in_flight(bus, holder):
            return Busy(_IN_FLIGHT)
        bus.hosted_turn_principal = claims.sub
        bus.hosted_turn_generation += 1
        GENERATION.set(bus.hosted_turn_generation)
        bus.hosted_turn_ops = tuple(claims.ops)
        bus.hosted_turn_secret = secrets.token_urlsafe(32)
        if hasattr(bus, "hosted_secret_history"):
            bus.hosted_secret_history.append(bus.hosted_turn_secret)
        bus.hosted_turn_live = True
    return None


def admit_collaborator(bus, holder, claims, *, now: Optional[float] = None) -> Optional[str]:
    """Admit fresh, separately verified handover claims; never call for a browser frame."""
    return _admit_claims(bus, holder, claims, collaborator=True, now=now)


def abandon_admission(bus, generation) -> None:
    """Release a grant that failed before the turn was logged or submitted."""
    with bus.hosted_lock:
        if bus.hosted_turn_live and bus.hosted_turn_generation == generation:
            bus.hosted_turn_live = False
            bus.hosted_turn_ops = ()
            bus.hosted_turn_secret = None
            bus.hosted_turn_generation += 1
    if GENERATION.get() == generation:
        GENERATION.set(None)


def admit_internal(bus, holder) -> Optional[str]:
    """Admit a system action under the launch grant, not a fabricated caller."""
    return _admit_worker(bus, holder)


def holds(bus, principal: Optional[str]) -> bool:
    """Whether ``principal``'s authority is still the live one."""
    return bool(
        principal is not None
        and not bus.hosted_fenced
        and bus.hosted_turn_live
        and bus.hosted_turn_principal == principal
    )


def _begin_stop(bus, session):
    """Start stopping ``session``'s turn and count it until it has settled."""
    bus.hosted_stopping += 1
    task = asyncio.get_running_loop().create_task(asyncio.wait_for(_stop(session), timeout=15.0))

    def settled(done):
        bus.hosted_stopping -= 1
        # Only an explicit True proves the old backend stopped. None, another
        # value, an exception or cancellation leaves it possibly alive.
        if done.cancelled() or done.exception() is not None or done.result() is not True:
            bus.hosted_fenced = True

    task.add_done_callback(settled)
    return task


def _stop(session):
    """Stop the turn the grant belonged to: bounded and fail-closed where the
    backend can (``stop_for_revocation``), a plain interrupt otherwise."""
    stop = getattr(session, "stop_for_revocation", None)
    return stop() if stop is not None else session.interrupt()


def interrupt(bus, session):
    """Invalidate queued/in-flight work and stop it before any successor."""
    bus.hosted_cancel_generation += 1
    with bus.hosted_lock:
        bus.hosted_turn_live = False
        bus.hosted_turn_ops = ()
        bus.hosted_turn_secret = None
        bus.hosted_turn_generation += 1
    if session is not None:
        return _begin_stop(bus, session)
    return None


def revoke(bus, session) -> None:
    """Permanently revoke the job, including its private bridge."""
    with bus.hosted_lock:
        bus.hosted_worker = None
        bus.hosted_fenced = True
    interrupt(bus, session)


def maintenance_checker(bus):
    """Fence a single idle maintenance call without granting a turn or tools."""
    with bus.hosted_lock:
        generation = bus.hosted_cancel_generation
        worker = bus.hosted_worker

    def check() -> Optional[str]:
        with bus.hosted_lock:
            if (
                worker is not None
                and bus.hosted_worker == worker
                and bus.hosted_cancel_generation == generation
                and not bus.hosted_fenced
                and not bus.hosted_quiescing
                and not bus.hosted_stopping
                and not bus.hosted_turn_live
            ):
                return None
        return "the hosted worker is no longer idle or authorised for maintenance"

    return check


def checker(bus):
    """A thread-safe ``() -> reason | None`` for the send of ONE turn: bound to
    the grant generation of the task asking (``GENERATION``), and so false for
    this turn's prompt once that grant is interrupted or replaced, even
    if another principal holds a newer one. The backend asks it at the send
    itself, in the thread that makes the provider call, after every await that
    could have let the grant end (recording, image expansion, a busy executor).
    Not set (no grant for this task) refuses."""
    generation = GENERATION.get()

    def check() -> Optional[str]:
        # Under the lock admit/interrupt take, so the check never sees a
        # half-written grant. It orders the CHECK only, not the provider
        # call that follows it: that is made after the lock is dropped (never
        # held across a blocking call), so an interrupt can still land between the
        # check and the prompt reaching omp, and its interrupt then queues
        # behind the prompt on the backend's single worker.
        with bus.hosted_lock:
            if (
                generation is not None
                and bus.hosted_turn_generation == generation
                and bus.hosted_turn_live
                and not bus.hosted_fenced
            ):
                return None
        return "your access to this project ended before the message was sent to the agent"

    return check
