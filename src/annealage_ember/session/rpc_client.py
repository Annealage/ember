"""``LeanRpcClient``: ``omp_rpc.RpcClient`` that does not archive streamed message updates.

omp's ``message_update`` notification carries the whole message so far (``message``)
and, inside ``assistantMessageEvent``, the same again (``partial``), for every streamed
token. ``RpcClient`` deep-clones each notification into an event history (default
10,000 entries) that ``prompt_and_wait``, ``wait_for_idle`` and ``collect_events`` read
back, so a long reply costs O(updates x reply size) of resident memory for as long as
the client lives (measured on a deployed Loom: 4,000 updates, a 62.5 KiB final
message, 251.87 MiB retained, 0.18 MiB after ``clear``), and a turn of more than
10,000 updates overflows the history and makes ``collect_events`` fail.

The one change: a ``message_update`` is not archived. Listeners still receive it, built
from the full notification before this is reached, and it is still counted
(``observability.rpc_event``). Every other notification is archived exactly as upstream
does, deep-cloned, so a ``message_end`` (the reply's full content and tool-call
arguments), ``agent_end`` (including the compact form ``message_count`` describes,
rebuilt from the retained ``message_end`` entries), tool events and errors are what
``prompt_and_wait`` / ``collect_events`` always returned. What is different is only that
``PromptTurn.events`` / ``collect_events()`` has no ``message_update`` entries, and a long
turn is no longer bounded by its token count. Ember reads a turn through listeners (``OmpSession``)
and ``on_event``, never through those readers; a consumer that needs the streamed
updates out of ``collect_events`` must use ``omp_rpc.RpcClient`` itself.
``max_event_history`` keeps upstream's default.

``stop()`` also empties the archive and the listener lists, so a stopped client keeps
nothing a session or a turn put in it.

The retained events and their estimated size (the length of their UTF-8 encoded JSON,
not of the Python objects, and not of the bytes omp sent) are exposed through
``metrics_snapshot`` for the lifecycle gauges (``observability``).

**Pinned private surface.** omp-rpc is a third-party package installed from a git
commit (``PINNED_OMP_RPC``). This subclass reaches these names of it and no others:
``omp_rpc.client._clone_json_object`` and ``RpcProcessExitError``; on ``RpcClient``:
``_write_lock`` and ``_write_json`` (re-implemented under the lock for the send gate), ``_append_event``,
``_events`` (``limit``, ``items``, ``offset``, ``clear``, ``current_index``,
``snapshot``, ``snapshot_from``, as ``_BoundedHistory`` has them), ``_event_condition``,
``_state_lock``, ``_pending``, ``_pending_host_tool_calls``,
``_pending_host_uri_requests``, ``_process`` and the listener lists in
``LISTENER_ATTRS``. ``__init__`` refuses, with a clear error, when any is missing or
the wrong shape, rather than archiving nothing or everything, and the unit tests read
the same names. Re-read ``omp_rpc/client.py`` against this file when the pin moves.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import threading
from typing import Any, Callable, Dict, Optional

from omp_rpc import RpcClient
from omp_rpc.client import RpcProcessExitError, _clone_json_object

from .. import observability

#: The omp-rpc commit this was written against (CI's install line in test.yml).
PINNED_OMP_RPC = "71c5eec978b0e7ce9ff057eb4e311f67f4f03eb9"

#: The notification type this client does not archive.
UNARCHIVED_EVENT = "message_update"

#: ``RpcClient``'s list-valued listener registries (``_typed_event_listeners`` is a dict of lists).
LISTENER_ATTRS = (
    "_notification_listeners",
    "_event_listeners",
    "_ready_listeners",
    "_unknown_notification_listeners",
    "_ui_request_listeners",
    "_extension_error_listeners",
    "_protocol_error_listeners",
    "_listener_error_listeners",
)
_TYPED_LISTENERS = "_typed_event_listeners"
_HISTORY_METHODS = (
    "limit",
    "items",
    "offset",
    "clear",
    "current_index",
    "snapshot",
    "snapshot_from",
)
_REQUIRED_ATTRS = (
    "_event_condition",
    "_state_lock",
    "_pending",
    "_pending_host_tool_calls",
    "_pending_host_uri_requests",
    "_process",
    "_write_lock",
    "_pgid",
    "_write_json",
    *LISTENER_ATTRS,
    _TYPED_LISTENERS,
)


class _SizedHistory:
    """``omp_rpc.client._BoundedHistory``'s interface, plus the estimated encoded size of what it holds."""

    def __init__(self, limit):
        self.limit = limit
        self.items = []
        self.sizes = []
        self.offset = 0
        self.estimated_bytes = 0

    def clear(self):
        self.items.clear()
        self.sizes.clear()
        self.offset = 0
        self.estimated_bytes = 0

    def append(self, item, size):
        self.items.append(item)
        self.sizes.append(size)
        self.estimated_bytes += size
        if self.limit is not None and len(self.items) > self.limit:
            trim = len(self.items) - self.limit
            self.estimated_bytes -= sum(self.sizes[:trim])
            del self.items[:trim]
            del self.sizes[:trim]
            self.offset += trim

    def current_index(self):
        return self.offset + len(self.items)

    def snapshot(self):
        return tuple(self.items)

    def snapshot_from(self, start_index):
        return tuple(self.items[start_index - self.offset :])


def estimated_size(payload: Dict[str, Any]) -> int:
    """The length in bytes of ``payload`` as compact UTF-8 JSON: an estimate of the history's
    share, not of the Python objects and not of the line omp sent. ``payload`` came from JSON,
    so an unsupported type is a bug and raises."""
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


#: Commands that make omp start or feed agent work: the ones a revoked grant must not send.
_WORK_COMMANDS = frozenset({"prompt", "abort_and_prompt", "steer", "follow_up"})


class SendRefused(RuntimeError):
    """The send gate refused a command at the write; nothing went to omp."""


class LeanRpcClient(RpcClient):
    """``RpcClient`` whose event history leaves out ``message_update`` and reports what it holds."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._gate_local = threading.local()
        events = getattr(self, "_events", None)
        missing = [name for name in _REQUIRED_ATTRS if not hasattr(self, name)]
        if (
            missing
            or not callable(getattr(self, "_append_event", None))
            or events is None
            or not all(hasattr(events, name) for name in _HISTORY_METHODS)
            or not all(isinstance(getattr(self, name), list) for name in LISTENER_ATTRS)
            or not isinstance(getattr(self, _TYPED_LISTENERS), dict)
        ):
            raise RuntimeError(
                "the installed omp-rpc does not match the one LeanRpcClient was written "
                "against (%s): %s changed shape"
                % (PINNED_OMP_RPC, ", ".join(missing) or "its event history")
            )
        self._events = _SizedHistory(events.limit)
        observability.track_client(self)

    @contextlib.contextmanager
    def send_gate(self, check: Optional[Callable[[], Optional[str]]]):
        """Within this block, a command that starts agent work (``prompt``,
        ``abort_and_prompt``, ``steer``, ``follow_up``) made on THIS thread asks
        ``check()`` immediately before its bytes are written, while holding
        the write lock, and is refused with ``SendRefused`` (nothing written)
        when it returns a reason. A command that is written is followed on
        the wire, never preceded, by any ``abort`` issued after it, because
        that abort's own write waits for the same lock. The wait for omp's
        response stays outside the lock, as upstream has it."""
        local = self._gate_local
        previous = getattr(local, "check", None)
        local.check = check
        try:
            yield
        finally:
            local.check = previous

    def kill(self) -> bool:
        """End omp and every descendant now, from any thread: SIGKILL to the
        process group omp leads (it is spawned in its own session, so the group
        is its own and holds the agent's tool children too, which could
        otherwise keep the pipes open), falling back to the leader alone. Takes
        no lock, so it works while a write is stalled on a full pipe. Waits up
        to a second to confirm the leader is gone; returns whether it is.
        Never raises."""
        process = self._process
        if process is None:
            return True
        try:
            pgid = getattr(self, "_pgid", None)
            killpg = getattr(os, "killpg", None)
            if pgid is not None and killpg is not None:
                try:
                    killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=1.0)
            return True
        except Exception:
            return False

    def _write_json(self, process, payload):
        check = getattr(self._gate_local, "check", None)
        if check is None or payload.get("type") not in _WORK_COMMANDS:
            return super()._write_json(process, payload)
        if process.stdin is None:
            raise RpcProcessExitError("RPC process stdin is unavailable")
        with self._write_lock:
            reason = check()
            if reason:
                raise SendRefused(reason)
            try:
                process.stdin.write(json.dumps(payload))
                process.stdin.write("\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RpcProcessExitError(f"Failed to write RPC command: {exc}") from exc

    def _append_event(self, payload: Dict[str, Any]) -> None:
        observability.rpc_event(payload.get("type"))
        if payload.get("type") == UNARCHIVED_EVENT:
            return
        archived = _clone_json_object(payload)
        size = estimated_size(archived)
        with self._event_condition:
            self._events.append(archived, size)
            self._event_condition.notify_all()

    def stop(self) -> None:
        try:
            super().stop()
        finally:
            with self._event_condition:
                self._events.clear()
            for name in LISTENER_ATTRS:
                getattr(self, name).clear()
            getattr(self, _TYPED_LISTENERS).clear()

    def metrics_snapshot(self) -> Dict[str, int]:
        """Counts of what this client holds, read consistently under its own locks; no content.

        ``pid`` is the running child's, or 0 when there is none."""
        with self._event_condition:
            retained = len(self._events.items)
            retained_bytes = self._events.estimated_bytes
        with self._state_lock:
            pending = len(self._pending)
        process = self._process
        listeners = sum(len(getattr(self, name)) for name in LISTENER_ATTRS) + sum(
            len(group) for group in list(getattr(self, _TYPED_LISTENERS).values())
        )
        return {
            "pid": process.pid if process is not None and process.poll() is None else 0,
            "retained_events": retained,
            "retained_estimated_bytes": retained_bytes,
            "listeners": listeners,
            "pending_requests": pending,
            "pending_host_calls": len(self._pending_host_tool_calls)
            + len(self._pending_host_uri_requests),
        }


def check() -> None:
    """Build a ``LeanRpcClient`` without starting omp: raises ``RuntimeError`` when the installed omp-rpc
    is not the shape this was written against. For a worker image's build or test step, so a pin
    mismatch fails there instead of on the live page: ``python -m annealage_ember.session.rpc_client``."""
    client = LeanRpcClient()
    client.stop()


if __name__ == "__main__":
    check()
    print("omp-rpc matches the shape LeanRpcClient was written against (%s)" % PINNED_OMP_RPC)
