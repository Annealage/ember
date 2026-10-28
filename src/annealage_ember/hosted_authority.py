"""Whose authority a hosted agent's running turn holds.

One agent serves a whole project, so every principal's turns share one bus and
the bus carries the authority that tool calls are checked against
(``hosted_turn_ops``/``_exp``/``_secret``/``_live``). That is only sound while a
single principal owns it: a second principal's turn frame used to overwrite it
under the first one's running, queued or deferred turn. ``admit`` is the one
place a hosted turn takes the authority, and it refuses a different principal
while any turn is in flight; ``holds`` is what a turn waiting to be delivered
must still satisfy when it is. Both fail closed.
"""

from __future__ import annotations

import secrets
import time
from typing import Optional


def _in_flight(bus, holder) -> bool:
    if bus.hosted_turn_live:
        return True
    return bool(holder is not None and holder.turn_in_flight())


def admit(bus, holder, claims, *, now: Optional[float] = None) -> Optional[str]:
    """Give ``claims``' principal the bus's turn authority for a turn about to
    be handed to the session, or return why it cannot have it (nothing changed).

    A turn from the principal that already owns the in-flight turns keeps what
    the owner has: the new ops can only narrow the live ones, and the live
    secret stays valid. Anyone else, or an owner nobody recorded, waits."""
    now = time.time() if now is None else now
    if claims is None or now >= claims.exp:
        return "your access to this project has expired, so this message was not sent"
    ops = tuple(claims.ops)
    secret = secrets.token_urlsafe(32)
    if _in_flight(bus, holder):
        if bus.hosted_turn_principal != claims.sub:
            return "another person's turn is in progress, so this message was not sent; wait for it to finish"
        if bus.hosted_turn_live and time.time() < bus.hosted_turn_exp:
            ops = tuple(op for op in bus.hosted_turn_ops if op in ops)
            secret = bus.hosted_turn_secret or secret
    bus.hosted_turn_principal = claims.sub
    bus.hosted_turn_ops = ops
    bus.hosted_turn_exp = claims.exp
    bus.hosted_turn_secret = secret
    bus.hosted_turn_live = True
    return None


def holds(bus, principal: Optional[str], *, now: Optional[float] = None) -> bool:
    """Whether ``principal``'s authority is still the live, unexpired one."""
    now = time.time() if now is None else now
    return bool(
        principal is not None
        and bus.hosted_turn_live
        and bus.hosted_turn_principal == principal
        and now < bus.hosted_turn_exp
    )
