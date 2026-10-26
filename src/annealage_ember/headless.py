"""One private conversation, no human: ``run_prompt``.

A product that wants a sub-agent for a batch step (review a sheet, summarise
a file) does not want a chat pane, a browser, a project directory of session
state or an approval card. ``run_prompt`` gives it the rest of the agent layer
without those: it starts a session on the Claude or omp backend, sends one
user message, waits for the turn to end, and returns what the model said, what
the run cost and which tools it called. Optional bounded follow-ups stay in
that same live conversation; by default it still sends exactly one turn.

    result = await run_prompt(
        "Check the sheet against the datasheet.",
        tools=ToolSet(tools=[...], grading=Grading(read=(...), view=(), write=(...)),
                      grant=("submit_result",)),
        backend="omp", model="provider/model", cwd=scratch_dir, timeout=300,
    )

What is private to the run. It builds its own ``ViewerBus`` (over a registry
nobody connects to) and its own ``ToolServer`` from the ``ToolSet``; it touches
no viewer registry, event log, ``sessions`` state or ``permissions.toml``. By
default session state is temporary and removed at return; ``state_dir`` opts
into durable OMP native state and explicit resume. ``cwd`` is the caller's
scratch working directory; make it empty, since the model is told nothing
about it and, on the Claude backend, cannot read it either (see below).

How permission works with nobody to ask. ``session.permissions.HeadlessBroker``
stands in for the broker: it never opens a request, so nothing waits on a card.
A call it is asked about is allowed only if its name is in the ``ToolSet``'s
``grant``, and denied at once, with a message that says why, otherwise. Read
and view tools never reach it on either backend (Claude's ``allowed_tools``;
omp's host-tool gate only asks about write-grade tools), so grading decides:
read and view are pre-allowed, a write-grade tool is refused unless granted.
A grant covers this run only and is never remembered.

Claude's built-ins. The session is built with an empty built-in tool list
(``--tools ""``), so the model has no Bash, Write, Read or the rest, only the
``ToolSet``'s tools; ``allowed_tools`` is the pre-allowed set plus the grants;
no settings file is loaded (``setting_sources`` empty), so nothing under
``cwd`` or in the user's own configuration adds a rule, a hook or a tool; the
shell sandbox is off since there is no shell. ``HeadlessBroker`` is the backstop
should anything still ask. omp already runs with every built-in disabled
(``--no-tools --no-extensions``), so its only tools are the ``ToolSet``'s,
unless the caller opts in with ``builtin_tools`` (see ``run_prompt``).
Codex is refused: it reaches tools through the app's ``/mcp`` endpoint, which
a headless run has no listener for.

Failure. A model or backend failure (the agent would not start, its package is
not installed, the model errored, the process died, the turn timed out) comes
back as ``RunResult.error``, with whatever text arrived first; ``run_prompt``
raises only for a bad argument (``ValueError``) or because the caller
cancelled it (``CancelledError``, after the session is closed).

When a run ends early. An unrecovered timeout or a cancel ends the run: the
broker refuses any further call, each tool handler starts refusing ("this run has ended") and
those running are cancelled and waited for (up to ``_DRAIN_TIMEOUT``, so their
cleanup is done before ``run_prompt`` returns), the backend process is killed
(omp's ``kill``, so a start still waiting on the process cannot hold the close
up), and the close itself is bounded by ``_CLOSE_TIMEOUT``. A handler that
is blocked inside a synchronous call, or past its last ``await``, cannot be
stopped and finishes on its own thread; keep per-run handlers free of that.

What a run leaves behind. By default, nothing of its own: session state is
temporary. An explicit ``state_dir`` retains OMP conversation state on every
exit. On Claude the CLI always uses ``--no-session-persistence``; durable
headless resume is not supported there.

Tools that ask the human themselves (``tools.asks_the_human``) are refused by
``ToolSet``: with no human they could never be answered, and the tool server
would otherwise file them under read.

Imported on demand (the package's headless API names resolve lazily), because
it imports the Claude SDK through ``tools``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import math
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

from . import product
from .files import atomic_replace, read_fixed_file, safe_fixed_file
from .hosted_state import UsageRecorder
from .session.base import (
    AGENT_READY,
    AGENT_UNAVAILABLE,
    AgentError,
    AgentRuntimeResolved,
    AgentStatus,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnEnd,
)
from .session.permissions import HeadlessBroker
from .tools import _ASKS_THE_HUMAN, Grading, ToolServer, _verify, fail, namespaced
from .viewers import ViewerBus, ViewerRegistry

#: The backends ``run_prompt`` can drive.
BACKENDS = ("omp", "claude")

#: The omp built-in tools a caller may enable with ``builtin_tools``: the
#: network-read ones, by omp's own names (omp 18.4.5 has no ``fetch`` built-in;
#: it refuses an unknown name in ``--tools``). Nothing that writes, runs code or
#: drives a browser is on this list, and it is not widened by configuration.
BUILTIN_TOOLS = ("web_search",)

#: OMP's configured model roles available to a headless child.
MODEL_ROLES = ("smol", "default", "slow")

#: The common thinking levels supported by both backends.
THINKING_LEVELS = ("low", "medium", "high")
BACKEND_THINKING_LEVELS = {
    "claude": THINKING_LEVELS,
    "omp": (*THINKING_LEVELS, "xhigh"),
}

#: What a pause-gated tool answers while the view is paused. A headless run has
#: no human to pause it, so it is never said; ``ToolServer`` requires it.
_PAUSED_MESSAGE = "this run has no product owner watching and cannot be paused"

#: Seconds each step of closing a session (``kill``, then ``close``) may take
#: before ``run_prompt`` gives up waiting for it and returns anyway.
_CLOSE_TIMEOUT = 10.0

#: Seconds ``run_prompt`` waits for the tool handlers it cancelled to finish
#: their cleanup before it returns.
_DRAIN_TIMEOUT = 5.0


@dataclasses.dataclass(frozen=True)
class ToolSet:
    """The tools one headless run may use.

    ``tools`` are ``@tool`` definitions (what a product's ``build_tools``
    passes to ``ToolServer``), ``grading`` the ``tools.Grading`` sorting every
    one of them, and ``grant`` the write-grade names pre-granted for this run.
    Built per call: the handlers close over the caller's per-run data.

    ``ValueError`` for a ``tools``/``grading`` pair ``ToolServer`` would refuse
    (a tool not graded, a graded name with no tool, a name in two grades, a
    name declared twice), or for a ``grant`` entry that is not a write-grade
    tool in the set: a read or view tool needs no grant, and one that is not
    there grants nothing. Needs an installed product (``product.install``), as
    ``ToolServer`` does.
    """

    tools: Tuple[Any, ...]
    grading: Grading
    grant: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "tools", tuple(self.tools))
        grant = (self.grant,) if isinstance(self.grant, str) else self.grant
        object.__setattr__(self, "grant", tuple(grant))
        if not isinstance(self.grading, Grading):
            raise ValueError("grading must be a tools.Grading, not %r" % (self.grading,))
        # Outside the try: no installed product is not a bad tool set.
        product.current()
        try:
            _verify(self.tools, self.grading)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from None
        asking = sorted(t.name for t in self.tools if getattr(t.handler, _ASKS_THE_HUMAN, False))
        if asking:
            raise ValueError(
                "tool(s) %s ask the product owner themselves (tools.asks_the_human), and a "
                "headless run has nobody to ask; leave them out of the set" % ", ".join(asking)
            )
        for name in self.grant:
            if name in self.grading.write:
                continue
            if name in self.grading.read or name in self.grading.view:
                raise ValueError(
                    "grant names %r, which is a read or view tool and runs without a grant; "
                    "only write-grade tools are granted" % name
                )
            raise ValueError(
                "grant names %r, which is not a tool in this set; only write-grade tools "
                "in the set are granted" % name
            )

@dataclasses.dataclass(frozen=True)
class RecoveryInfo:
    """Observed recovery capability, never a promise that a file is complete.

    ``resume_supported`` describes native backend support; ``conversation_file``
    is populated only after the backend reports it. Explicit resume still
    verifies the file and native switch before sending any new input.
    """

    backend: str
    session_id: str
    state_dir: Optional[str]
    conversation_file: Optional[str]
    resume_supported: bool
    live_recovery_supported: bool


@dataclasses.dataclass(frozen=True)
class TimeoutContext:
    """One timeout observation. Only a safe work interval permits continuation.

    ``result`` can contain confidential partial text and tool errors. Do not
    log it indiscriminately. ``remaining`` is the hard deadline's remaining
    seconds; elapsed includes startup, diagnostics and callbacks.
    """

    kind: str
    result: RunResult
    elapsed: float
    remaining: float
    interruption: str
    recovery: RecoveryInfo
    rpc_operation: Optional[str] = None



@dataclasses.dataclass(frozen=True)
class RunResult:
    """What one headless run produced.

    ``text`` is what the model said after its last tool result (the whole
    reply when it called no tool; the narration before and between tool calls
    is dropped), and what had arrived so far when the run failed or timed out.
    A turn that ends on a tool call (``ended_by_tool``) has no text.
    ``stop_reason`` is the backend's (``end_turn``, ``max_tokens``, ...) or
    ``ended_by_tool``, ``rejected``, ``timeout`` or ``error``. ``cost_usd`` and
    ``tokens`` sum reported turn figures across the run; a dimension unknown
    in any turn remains ``None``, never a fabricated zero. The callback gets
    only that turn's figures. ``error`` is ``None`` for a clean run, else why
    not (``"timeout"`` for a timeout). Text and stop reason describe the last
    turn, including any partial reply on failure.

    ``role_resolution_error`` is true only when OMP rejected the requested
    role before any provider request.

    ``calls`` is every tool the model called, in order, as ``(name, ok)``:
    the ``ToolSet``'s own name on both backends, and ``ok`` false for a call
    that failed, was refused, or never returned. ``tool_errors`` contains
    ``(name, text)`` for failed tool results, with the exact returned text,
    including schema/permission failures. No arguments or successful results
    are kept. Error text may be confidential: it is excluded from ``repr``
    and must not be logged indiscriminately.
    """

    text: str
    stop_reason: str
    cost_usd: Optional[float]
    tokens: Optional[dict]
    calls: Tuple[Tuple[str, bool], ...] = ()
    native_max_turns: Optional[int] = None
    native_num_turns: Optional[int] = None
    native_turns_supported: Optional[bool] = None
    error: Optional[str] = None
    role_resolution_error: bool = False
    tool_errors: Tuple[Tuple[str, str], ...] = dataclasses.field(default=(), repr=False)
    timeout_kind: Optional[str] = None
    recovery: Optional[RecoveryInfo] = None


class _Run:
    """The current turn and completed turns of one live run. The guard and
    event sink remain attached to the same object throughout the run."""

    def __init__(self, server_prefix: str, on_runtime=None, on_activity=None):
        self._prefix = server_prefix
        self._on_runtime = on_runtime
        self._on_activity = on_activity
        self.done = asyncio.Event()
        self.text: List[str] = []
        self.errors: List[str] = []
        self.stop_reason: Optional[str] = None
        self.cost_usd: Optional[float] = None
        self.tokens: Optional[dict] = None
        # [name, ok] per call in the order made, and where each call id is.
        self._calls: List[list] = []
        self._by_id: dict = {}
        self._tool_errors: List[Tuple[str, str]] = []
        self._completed: List[RunResult] = []
        self._last_turn: Optional[int] = None
        self._native_num_turns: Optional[int] = None
        # Set when the run is over, however it ended; a tool handler that starts
        # after that refuses (``guard``), and ``end`` cancels those running.
        self.over = False
        self._inflight: set = set()
        self.role_resolution_error = False
        self.timeout_kind = None
        self.paused = False
        self._timeout_interrupt_errors = []
        self._timeout_interrupt_acknowledged = False
        self._pending_calls = set()
        self._tool_results_changed = asyncio.Event()
        self.recovery = None
        self.reserve_turn = None
        # The session, for what only it knows about how its turn ended.
        self.session: Any = None

    @property
    def finished(self) -> bool:
        return self.done.is_set()

    def guard(self, tool_def):
        """``tool_def`` with its handler made to refuse once the run is over,
        and to be cancelled (at its next ``await``) by ``end``. A late tool call
        from the backend (omp runs each on its own thread and can outlive its
        timeout) then cannot touch the caller's per-run state after
        ``run_prompt`` has returned. A handler already past its last ``await``
        cannot be stopped, and one blocking inside a synchronous call runs on."""
        inner = tool_def.handler

        async def handler(args):
            if self.over:
                return fail("this run has ended, so %s did not run" % tool_def.name)
            if self.paused:
                return fail("this run is interrupted, so %s did not run" % tool_def.name)
            task = asyncio.current_task()
            self._inflight.add(task)
            try:
                return await inner(args)
            finally:
                self._inflight.discard(task)

        return dataclasses.replace(tool_def, handler=handler)

    def end(self) -> None:
        """The run is over: refuse new tool calls and cancel the running ones.
        Idempotent; ``drain`` waits for the cancellations to finish."""
        if self.over:
            return
        self.over = True
        for task in list(self._inflight):
            task.cancel()

    async def drain(self) -> None:
        """Wait, up to ``_DRAIN_TIMEOUT``, for the handlers ``end`` cancelled to
        finish their cleanup, so none runs on after ``run_prompt`` returns.
        One that will not stop in that time is abandoned."""
        pending = [t for t in self._inflight if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=_DRAIN_TIMEOUT)

    async def settle_tools(self) -> None:
        """Do not inspect submission state while a tool can still change it.
        The enclosing whole-run timeout bounds this wait, without cancelling
        a successful write merely because the turn boundary arrived first."""
        while self._inflight:
            await asyncio.wait(self._inflight)

    async def settle_tool_results(self) -> None:
        """Wait until native tool results have caught up with host handlers."""
        await self.settle_tools()
        while self._pending_calls:
            self._tool_results_changed.clear()
            await self._tool_results_changed.wait()

    def next_turn(self) -> None:
        """Reset only turn-local collection, never the live tool guard."""
        self._completed.append(self.turn_result())
        self.done.clear()
        self.text.clear()
        self.errors.clear()
        self._timeout_interrupt_errors.clear()
        self._timeout_interrupt_acknowledged = False
        self.stop_reason = None
        self.cost_usd = None
        self.tokens = None
        self._calls.clear()
        self._by_id.clear()
        self._tool_errors.clear()
        self.role_resolution_error = False
        self._pending_calls.clear()
        self.timeout_kind = None
        self._native_num_turns = None

    def error(self) -> Optional[str]:
        errors = self.errors
        if not self._timeout_interrupt_acknowledged and self._timeout_interrupt_errors:
            errors = [*errors, *self._timeout_interrupt_errors]
        return "; ".join(errors) or None

    def calls(self) -> Tuple[Tuple[str, bool], ...]:
        return tuple((name, ok) for name, ok in self._calls)

    def on_event(self, event) -> None:
        if self.over:
            return
        if self.finished and not isinstance(
            event, (AgentRuntimeResolved, AgentError, AgentStatus, ToolResult)
        ):
            return
        if (
            self._completed
            and isinstance(event, (TextDelta, ToolUse, TurnEnd))
            and self._last_turn is not None
            and event.turn <= self._last_turn
        ):
            return
        # Project only public model text and tool identity/outcome, never the
        # session event itself (which can carry arguments or private results).
        activity = None
        if isinstance(event, TextDelta):
            activity = {"kind": "text_delta", "text": event.text, "turn": event.turn}
        elif isinstance(event, ToolUse):
            name = event.name
            if name.startswith(self._prefix):
                name = name[len(self._prefix):]
            activity = {"kind": "tool_use", "name": name,
                        "tool_use_id": event.tool_use_id, "turn": event.turn}
        elif isinstance(event, ToolResult):
            if not self._completed or event.tool_use_id in self._by_id:
                activity = {"kind": "tool_result", "tool_use_id": event.tool_use_id,
                            "is_error": event.is_error}
        if activity is not None and self._on_activity is not None:
            try:
                self._on_activity(activity)
            except Exception:
                # Observability must not change execution or leak event data
                # through an observer exception's repr.
                sys.stderr.write("warning: headless activity observer failed\n")
        if isinstance(event, AgentRuntimeResolved):
            if self._on_runtime is not None:
                try:
                    self._on_runtime(event)
                except Exception as exc:
                    sys.stderr.write("warning: headless runtime observer failed: %r\n" % (exc,))
        elif isinstance(event, TextDelta):
            self.text.append(event.text)
        elif isinstance(event, ToolUse):
            name = event.name
            if name.startswith(self._prefix):
                name = name[len(self._prefix) :]
            self._by_id[event.tool_use_id] = len(self._calls)
            self._calls.append([name, False])
            self._pending_calls.add(event.tool_use_id)
        elif isinstance(event, ToolResult):
            self._pending_calls.discard(event.tool_use_id)
            self._tool_results_changed.set()
            index = self._by_id.get(event.tool_use_id)
            if self._completed and index is None:
                # ToolResult has no turn number. An old result must not clear
                # this turn's text or be attached to a new call.
                return
            # What was said so far was before the tool; the answer follows it.
            self.text.clear()
            if index is not None:
                self._calls[index][1] = not event.is_error
                if event.is_error:
                    self._tool_errors.append((self._calls[index][0], event.text))
        elif isinstance(event, AgentError):
            if event.role_resolution_error is True:
                self.role_resolution_error = True
            message = (event.stderr or event.remediation or "").strip()
            errors = (
                self._timeout_interrupt_errors
                if event.interruption_reason == "host_timeout_abort" else self.errors
            )
            if message and message not in errors:
                errors.append(message)
        elif isinstance(event, AgentStatus):
            if event.status == AGENT_UNAVAILABLE:
                # After this call returns: the session emits its AgentError
                # right behind the status change.
                asyncio.get_running_loop().call_soon(self._stopped)
        elif isinstance(event, TurnEnd):
            if event.stop_reason == "steered":
                # omp: another message redirected the turn; the real end follows.
                return
            self.stop_reason = event.stop_reason
            self._last_turn = event.turn
            self.cost_usd = event.cost_usd
            self.tokens = event.tokens
            self._native_num_turns = getattr(self.session, "last_result_num_turns", None)
            # A Claude result that said it failed (an API error, say) ends with
            # a TurnEnd like any other; the session keeps why, read here. Its
            # AgentError has usually added the same text already.
            failure = getattr(self.session, "last_result_error", None)
            if failure:
                if failure not in self.errors:
                    self.errors.append(failure)
                self.stop_reason = "error"
            self.done.set()

    def _stopped(self, why: str = "the agent stopped before answering") -> None:
        """The session went unavailable: no turn end is coming."""
        if self.finished:
            return
        self.stop_reason = "error"
        if not self.errors:
            self.errors.append(why)
        self.done.set()

    def turn_result(self, *, stop_reason: Optional[str] = None, error: Optional[str] = None):
        return RunResult(
            text="".join(self.text),
            stop_reason=stop_reason or self.stop_reason or "error",
            cost_usd=self.cost_usd,
            tokens=self.tokens,
            calls=self.calls(),
            native_max_turns=getattr(self.session, "_native_max_turns", None),
            native_num_turns=self._native_num_turns,
            native_turns_supported=getattr(self.session, "_native_turns_supported", None),
            error=error if error is not None else self.error(),
            role_resolution_error=self.role_resolution_error,
            tool_errors=tuple(self._tool_errors),
            timeout_kind=self.timeout_kind,
            recovery=self.recovery() if self.recovery is not None else None,
        )

    def result(self, *, stop_reason: Optional[str] = None, error: Optional[str] = None):
        current = self.turn_result(stop_reason=stop_reason, error=error)
        if not self._completed:
            return current
        turns = [*self._completed, current]
        token_keys = {key for turn in turns for key in (turn.tokens or {})}
        tokens = {
            key: (
                sum(turn.tokens[key] for turn in turns)
                if all(turn.tokens is not None and turn.tokens.get(key) is not None for turn in turns)
                else None
            )
            for key in token_keys
        } if token_keys else None
        return dataclasses.replace(
            current,
            cost_usd=(
                sum(turn.cost_usd for turn in turns)
                if all(turn.cost_usd is not None for turn in turns) else None
            ),
            tokens=tokens,
            calls=tuple(call for turn in turns for call in turn.calls),
            tool_errors=tuple(item for turn in turns for item in turn.tool_errors),
            native_num_turns=(
                sum(turn.native_num_turns for turn in turns)
                if all(turn.native_num_turns is not None for turn in turns) else None
            ),
        )

def _check_arguments(
    prompt,
    tools,
    backend,
    model,
    cwd,
    timeout,
    thinking,
    omp_request_timeout,
    provider_session_id=None,
    cache_retention=None,
    usage_sink=None,
    builtin_tools=(),
    on_runtime=None,
    max_budget_usd=None,
    native_max_turns=None,
    model_role=None,
    on_turn_result=None,
    max_followups=0,
):
    if backend == "codex":
        raise ValueError(
            "run_prompt does not support the codex backend: it reaches its tools "
            "through the app's /mcp endpoint, which a headless run has no listener "
            "for; use backend='omp' or 'claude'"
        )
    if backend not in BACKENDS:
        raise ValueError("backend must be one of %s, not %r" % (", ".join(BACKENDS), backend))
    if usage_sink is not None:
        if not callable(usage_sink):
            raise ValueError(
                "usage_sink must be a callable taking one dict, not %r" % (usage_sink,)
            )
        if backend != "omp":
            raise ValueError(
                "usage_sink is only supported with backend='omp': the Claude backend reports "
                "usage per turn, not per provider request"
            )
    for name, value in (
        ("provider_session_id", provider_session_id),
        ("cache_retention", cache_retention),
    ):
        if value is not None and backend != "omp":
            raise ValueError("%s is only supported with backend='omp'" % name)
    if provider_session_id is not None and (
        not isinstance(provider_session_id, str) or not provider_session_id.strip()
    ):
        raise ValueError("provider_session_id must be None or a non-empty string")
    if cache_retention not in (None, "short", "long", "none"):
        raise ValueError("cache_retention must be None, 'short', 'long' or 'none'")

    if on_runtime is not None and not callable(on_runtime):
        raise ValueError("on_runtime must be a callable taking one AgentRuntimeResolved event")
    if on_turn_result is not None and not callable(on_turn_result):
        raise ValueError("on_turn_result must be a callable taking one RunResult")
    if isinstance(max_followups, bool) or not isinstance(max_followups, int) or max_followups < 0:
        raise ValueError("max_followups must be a nonnegative integer")
    if not isinstance(tools, ToolSet):
        raise ValueError("tools must be a ToolSet, not %r" % (tools,))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("model must be None or a non-empty string")
    if model_role is not None:
        if model_role not in MODEL_ROLES:
            raise ValueError(
                "model_role must be one of %s or None, not %r"
                % (", ".join(MODEL_ROLES), model_role)
            )
        if model is not None:
            raise ValueError("model and model_role are mutually exclusive")
        if backend != "omp":
            raise ValueError("model_role is currently supported only with backend='omp'")
    if max_budget_usd is not None:
        if (backend != "claude" or isinstance(max_budget_usd, bool)
                or not isinstance(max_budget_usd, (int, float))
                or not math.isfinite(max_budget_usd) or max_budget_usd <= 0):
            raise ValueError("max_budget_usd requires a finite positive Claude-native budget")
    if native_max_turns is not None:
        if (backend != "claude" or isinstance(native_max_turns, bool)
                or not isinstance(native_max_turns, int) or native_max_turns <= 0):
            raise ValueError("native_max_turns requires a positive integer and backend='claude'")
    levels = BACKEND_THINKING_LEVELS[backend]
    if thinking is not None and thinking not in levels:
        raise ValueError(
            "thinking must be one of %s or None for %s, not %r" % (", ".join(levels), backend, thinking)
        )
    if isinstance(builtin_tools, (str, bytes)) or not isinstance(builtin_tools, (list, tuple)):
        raise ValueError("builtin_tools must be a list or tuple of tool names")
    if builtin_tools:
        if backend != "omp":
            raise ValueError("builtin_tools is only supported with backend='omp'")
        for name in builtin_tools:
            if name not in BUILTIN_TOOLS:
                raise ValueError(
                    "builtin_tools may name only %s, not %r" % (", ".join(BUILTIN_TOOLS), name)
                )
        if len(set(builtin_tools)) != len(builtin_tools):
            raise ValueError("builtin_tools names a tool twice")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("timeout must be a finite number of seconds greater than 0, not %r" % (timeout,))
    if omp_request_timeout is not None:
        if backend != "omp":
            raise ValueError("omp_request_timeout is only supported with backend='omp'")
        invalid_request_timeout = (
            isinstance(omp_request_timeout, bool)
            or not isinstance(omp_request_timeout, (int, float))
            or omp_request_timeout <= 0
        )
        if not invalid_request_timeout:
            try:
                invalid_request_timeout = not math.isfinite(omp_request_timeout)
            except OverflowError:
                invalid_request_timeout = True
        if invalid_request_timeout:
            raise ValueError(
                "omp_request_timeout must be a finite number of seconds greater than 0, "
                "not %r" % (omp_request_timeout,)
            )
    # A str or path only: os.path.isdir takes an int for a file descriptor.
    try:
        is_directory = isinstance(cwd, (str, os.PathLike)) and os.path.isdir(cwd)
    except ValueError:
        is_directory = False
    if not is_directory:
        raise ValueError("cwd must be an existing directory, not %r" % (cwd,))


def _build_session(
    backend,
    on_event,
    *,
    broker,
    server,
    allowed_tools,
    model,
    model_role,
    thinking,
    cwd,
    state_dir,
    instructions,
    omp_agent_dir,
    omp_session_dir,
    omp_binary,
    omp_config_dir,
    omp_request_timeout,
    session_id,
    on_request_usage,
    builtin_tools=(),
    max_budget_usd,
    native_max_turns,
    on_owner,
    owner_context,
    provider_session_id=None,
    cache_retention=None,
    resume=None,
    resume_turn=0,
    on_session_file=None,
):
    """The session ``backend`` names, built and not started. ``launch.build_session``
    is the app's: it is made of the served directory (the working directory and
    the state directory are one there), resume, settings and the product's own
    context, none of which a headless run has, so the two classes are built
    directly with only what a run needs."""
    if backend == "omp":
        from .session.omp import OmpSession

        return OmpSession(
            on_event,
            cwd=cwd,
            session_id=session_id,
            broker=broker,
            model=model,
            model_role=model_role,
            tool_table=server.host_tool_table(),
            thinking=thinking,
            instructions=instructions,
            agent_dir=omp_agent_dir,
            config_dir=omp_config_dir,
            binary=omp_binary,
            request_timeout=omp_request_timeout,
            # The conversation file goes under the run's own state, and goes
            # with it.
            session_dir=omp_session_dir or os.path.join(state_dir, "omp"),
            resume=resume,
            turn=resume_turn,
            strict_resume=resume is not None,
            on_session_file=on_session_file,
            provider_session_id=provider_session_id,
            cache_retention=cache_retention,
            on_request_usage=on_request_usage,
            builtin_tools=builtin_tools,
        )

    from .session.sdk import SdkSession
    return SdkSession(
        on_event,
        cwd=cwd,
        session_id=session_id,
        broker=broker,
        model=model,
        effort=thinking,
        mcp_servers=server.mcp_servers,
        allowed_tools=allowed_tools,
        instructions=instructions,
        # No shell to contain, no built-in tools, no settings files, and no
        # transcript left under ~/.claude/projects: the model has the ToolSet
        # and nothing else, and the run leaves nothing behind (module docstring).
        sandbox=False,
        builtin_tools=(),
        setting_sources=(),
        persist_session=False,
        max_budget_usd=max_budget_usd,
        native_max_turns=native_max_turns,
        on_owner=on_owner,
        owner_context=owner_context,
    )


async def _timeout_callback(callback, context):
    if callback is None:
        return None
    text = callback(context)
    if inspect.isawaitable(text):
        text = await text
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ValueError("on_timeout must return None or a non-empty string")
    return text


async def _drive(
    session, run: _Run, prompt: str, on_turn_result, max_followups: int, *,
    work_timeout=None, interrupt_timeout=10, on_timeout=None,
    max_timeout_recoveries=0, timeout_context=None, deadline=float("inf"),
) -> None:
    """Drive bounded turns without replacing the session or replaying tools."""
    async def deadline_gate():
        if run.over:
            raise asyncio.CancelledError
        if asyncio.get_running_loop().time() >= deadline:
            # Let the outer deadline owner classify and stop the run. An
            # overdue synchronous callback must never admit another turn
            # merely because the event loop has not serviced its timer yet.
            await asyncio.Future()

    async def submit(text, *, start=False):
        try:
            await deadline_gate()
            if start:
                await session.start()
            await deadline_gate()
            if run.over or session.agent_status() != AGENT_READY:
                run._stopped("the agent did not start" if start else "the agent stopped before follow-up")
                return
            if run.reserve_turn is not None:
                run.reserve_turn()
            await session.submit_turn([{"type": "text", "text": text}])
        except Exception as exc:
            run.errors.append("%s: %s" % (type(exc).__name__, exc))
            run.stop_reason = "error"
            run.done.set()

    async def boundary():
        await run.done.wait()
        # Backend death is already a terminal boundary. Its prompt/transport
        # coroutine may never acknowledge; do not let it mask the real error.
        if run.error() or session.agent_status() != AGENT_READY:
            return
        await asyncio.shield(submission)
        if run.error() or getattr(session, "last_request_timeout", None) is not None:
            return
        await run.settle_tools()

    ordinary = {"end", "end_turn", "success", "stop", "stop_sequence",
                "max_tokens", "length", "ended_by_tool"}
    submission = asyncio.ensure_future(submit(prompt, start=True))
    settled = None
    followups = recoveries = 0
    diagnostic_turn = False
    try:
        while True:
            settled = asyncio.ensure_future(boundary())
            await asyncio.wait({settled}, timeout=work_timeout)
            if not settled.done():
                # Refuse new host calls while allowing successful writes already
                # running to finish. Never cancel/replay them to recover time.
                run.paused = True
                if run.error():
                    return
                interrupt = getattr(session, "interrupt_for_timeout", None)
                outcome = "unsupported" if interrupt is None else "unsafe"
                if interrupt is not None:
                    async def stop_and_settle(
                        interrupt=interrupt, settled=settled, submission=submission,
                    ):
                        # A host write may already have committed. Let its
                        # result enter the native conversation BEFORE abort,
                        # not merely before the next user turn.
                        await run.settle_tool_results()
                        await deadline_gate()
                        if await interrupt() is not True:
                            return False
                        # Native abort errors may precede the RPC response.
                        # Only its positive acknowledgement makes the typed
                        # host-owned interruption expected rather than fatal.
                        run._timeout_interrupt_acknowledged = True
                        await asyncio.shield(submission)
                        await asyncio.shield(settled)
                        return (
                            not run.over and not run.error() and not run._pending_calls
                            and session.agent_status() == AGENT_READY
                            and run.stop_reason in ordinary | {"interrupted", "aborted"}
                        )

                    stopping = asyncio.ensure_future(stop_and_settle())
                    try:
                        await asyncio.wait({stopping}, timeout=interrupt_timeout)
                        if stopping.done() and not stopping.cancelled():
                            try:
                                if stopping.result() is True:
                                    outcome = "safe"
                            except Exception:
                                pass
                    finally:
                        await _cancelled(stopping)
                if (run.errors or (
                    run.finished and run.stop_reason not in ordinary | {"interrupted", "aborted"}
                )):
                    return
                await deadline_gate()
                run.timeout_kind = "work_interval"
                diagnostic = await _timeout_callback(
                    on_timeout, timeout_context("work_interval", outcome)
                )
                await deadline_gate()
                if (outcome != "safe" or diagnostic is None
                        or recoveries >= max_timeout_recoveries or run.over or run.error()
                        or session.agent_status() != AGENT_READY):
                    run.stop_reason = "timeout"
                    if not run.error():
                        run.errors.append("timeout")
                    return
                # The old turn has a real terminal boundary and no pending
                # tool/result/submission. Only now may another user turn exist.
                run.next_turn()
                run.paused = False
                recoveries += 1
                diagnostic_turn = True
                submission = asyncio.ensure_future(submit(diagnostic))
                continue
            await settled
            if getattr(session, "last_request_timeout", None) is not None:
                run.timeout_kind = "rpc_request_timeout"
                run.paused = True
                await deadline_gate()
                await _timeout_callback(
                    on_timeout, timeout_context("rpc_request_timeout", "not_attempted")
                )
                return
            if on_turn_result is None or run.error() or run.stop_reason not in ordinary:
                return
            if run.over or session.agent_status() != AGENT_READY:
                return
            await deadline_gate()
            correction = on_turn_result(run.turn_result())
            if inspect.isawaitable(correction):
                correction = await correction
            await deadline_gate()
            if correction is not None and (
                not isinstance(correction, str) or not correction.strip()
            ):
                raise ValueError("on_turn_result must return None or a non-empty string")
            if correction is None or (not diagnostic_turn and followups >= max_followups):
                return
            if run.over or run.error() or session.agent_status() != AGENT_READY:
                return
            run.next_turn()
            if not diagnostic_turn:
                followups += 1
            diagnostic_turn = False
            submission = asyncio.ensure_future(submit(correction))
    except Exception as exc:
        run.errors.append("%s: %s" % (type(exc).__name__, exc))
        run.stop_reason = "error"
        run.done.set()
    finally:
        if settled is not None:
            await _cancelled(settled)
        await _cancelled(submission)


class _ConversationState:
    """One locked durable native conversation; no prompts or tools are replayed."""

    def __init__(self, path, *, durable, resume, backend, cwd):
        self.path = Path(path)
        self.durable = durable
        self.manifest = self.path / "headless.json"
        if durable and safe_fixed_file(self.path, self.manifest.name) is None:
            raise ValueError("headless manifest must be a regular file with one link")
        self.error = None
        if resume:
            try:
                self.data = json.loads(read_fixed_file(self.path, self.manifest.name))
                if (self.data["version"] != 1 or self.data["backend"] != backend
                        or self.data["cwd"] != os.path.realpath(cwd)
                        or type(self.data["turn"]) is not int or self.data["turn"] < 0
                        or not isinstance(self.data["session_id"], str)
                        or not self.data["session_id"].startswith("headless-")
                        or not isinstance(self.data["conversation_file"], str)):
                    raise ValueError("incompatible headless state")
                conversation = Path(self.data["conversation_file"])
                conversation.resolve().relative_to((self.path / "omp").resolve())
                if not conversation.is_file():
                    raise ValueError("native conversation file is missing")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("cannot resume headless state: %s" % exc) from exc
        else:
            if durable and self.manifest.exists():
                raise ValueError("state_dir already contains a run; use resume=True explicitly")
            self.data = {
                "version": 1, "backend": backend, "cwd": os.path.realpath(cwd),
                "session_id": "headless-%s" % secrets.token_hex(8),
                "turn": 0, "conversation_file": None,
            }
            self.save()

    def save(self):
        if self.durable:
            try:
                atomic_replace(self.manifest, json.dumps(self.data).encode(), default_mode=0o600)
                directory = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as exc:
                self.error = exc
                raise

    def record_file(self, path):
        self.data["conversation_file"] = os.path.abspath(path)
        self.save()

    def reserve_turn(self):
        if self.error is not None:
            raise RuntimeError("conversation state could not be persisted") from self.error
        # Reserve BEFORE submitting, so a crash or uncertain acknowledgement
        # cannot reuse any provider usage event identity after explicit resume.
        self.data["turn"] += 1
        self.save()


@contextlib.contextmanager
def _conversation_state(state_dir, *, resume, backend, cwd):
    with contextlib.ExitStack() as stack:
        durable = state_dir is not None
        if durable:
            try:
                import fcntl
            except ImportError as exc:
                raise ValueError("durable headless state requires POSIX file locking") from exc

            path = Path(state_dir).absolute()
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = safe_fixed_file(path, "headless.lock")
            if lock_path is None:
                raise ValueError("headless lock must be a regular file with one link")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
            fd = os.open(lock_path, flags, 0o600)
            lock = stack.enter_context(os.fdopen(fd, "a+b"))
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid()):
                raise ValueError("headless lock is not privately owned")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("state_dir is already in use") from exc
        else:
            path = stack.enter_context(tempfile.TemporaryDirectory(prefix="annealage-headless-"))
        yield _ConversationState(path, durable=durable, resume=resume, backend=backend, cwd=cwd)


async def _cancelled(task: "asyncio.Future") -> None:
    """Cancel ``task`` and wait boundedly, even for a misbehaving callback."""
    if task.done():
        return
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=_DRAIN_TIMEOUT)
    if task in done:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    else:
        sys.stderr.write("warning: headless task did not stop within %gs\n" % _DRAIN_TIMEOUT)


async def _close(session, *, kill: bool) -> None:
    """Close ``session``, never for longer than ``_CLOSE_TIMEOUT``. ``kill``
    first ends a backend process that may be stuck behind a blocked call (omp
    mid-start), so the close has nothing to wait for."""
    try:
        if kill and hasattr(session, "kill"):
            await asyncio.wait_for(session.kill(), _CLOSE_TIMEOUT)
        await asyncio.wait_for(session.close(), _CLOSE_TIMEOUT)
    except asyncio.TimeoutError:
        sys.stderr.write("warning: headless session did not close within %gs\n" % _CLOSE_TIMEOUT)
    except Exception as exc:
        sys.stderr.write("warning: headless session did not close cleanly: %r\n" % (exc,))


async def run_prompt(
    prompt,
    *,
    tools,
    backend,
    model,
    cwd,
    timeout,
    system=None,
    thinking=None,
    model_role=None,
    omp_agent_dir=None,
    omp_session_dir=None,
    omp_binary=None,
    omp_config_dir=None,
    omp_request_timeout=None,
    provider_session_id=None,
    cache_retention=None,
    usage_sink=None,
    builtin_tools=(),
    on_runtime=None,
    on_activity=None,
    max_budget_usd=None,
    native_max_turns=None,
    on_owner=None,
    owner_context=None,
    on_turn_result=None,
    max_followups=0,
    work_timeout=None,
    interrupt_timeout=10,
    on_timeout=None,
    max_timeout_recoveries=0,
    state_dir=None,
    resume=False,
):
    """Run ``prompt`` in a fresh agent session with ``tools`` and return its
    ``RunResult``. One turn by default; see the module docstring.

    ``backend`` is ``"omp"`` or ``"claude"``; ``model`` optionally names omp's
    ``provider/model`` or a Claude model; ``None`` uses the backend default.
    Alternatively, ``model_role`` selects OMP's configured ``smol``, ``default``
    or ``slow`` role and cannot be combined with ``model``. OMP resolves the
    role from its configured model settings; the runtime observer reports the
    model OMP actually selected. Unsupported role resolution is exposed as
    ``RunResult.role_resolution_error`` only for OMP's exact preflight rejection.
    ``model_role`` is not currently supported by Claude because this package has
    no Claude role mapping. ``cwd`` is an existing directory, the caller's
    scratch space, used only as the backend's working directory. ``timeout`` is
    seconds for the whole run, start included: when it passes the session is
    closed and the result is ``error="timeout"``.
    ``omp_request_timeout`` is an optional per-request wait for OMP RPC calls,
    including the prompt acknowledgement. ``None`` leaves ``omp_rpc``'s 30-second
    default unchanged; it is separate from the whole-run ``timeout``.

    ``work_timeout`` optionally bounds each active turn (startup, prompt
    acknowledgement and tool settlement included). It never extends ``timeout``,
    the hard total deadline. OMP alone currently supports safe live recovery:
    abort must acknowledge, a real terminal boundary must arrive, the original
    submission must settle and all host tools/results must finish, within
    ``interrupt_timeout`` seconds. New tool calls are refused during settlement.
    Unknown/failed interruption fails closed. Claude reports unsupported.

    ``on_timeout(TimeoutContext) -> str | None`` may return a diagnostic user
    prompt ONLY after a ``work_interval`` with ``interruption="safe"``. At most
    ``max_timeout_recoveries`` such prompts are sent, on the identical live
    child. No original prompt or successful write is replayed. Its immediate
    clean ``on_turn_result`` continuation does not consume ``max_followups``.
    Hard-deadline and prompt-RPC timeouts are observational callbacks only;
    returned prompts are ignored. Callback failures are errors, never retries.
    Cancellation propagates and never invokes this callback. Auth, infra,
    provider/native limits and ordinary failures do not invoke it either.

    ``state_dir`` opts into durable OMP conversation files plus a private
    ``headless.json`` identity/turn manifest. On POSIX, an exclusive nonblocking
    file lock prevents concurrent use. Nothing there is deleted by this API,
    including on timeout/cancellation. ``resume=True`` explicitly opens that
    native conversation and sends ``prompt`` as a NEW user turn; it never
    replays the old task. Missing files, failed/mismatched native resumes and
    incompatible state fail closed before any input. The same SDK session id
    and reserved increasing turn numbers preserve usage event identities.
    Results aggregate this invocation's turns, not earlier invocations.
    Claude durable state/resume is not supported by this API. Without
    ``state_dir`` the default remains temporary state, deleted at return.
    Recovery metadata reports capability, not proof of native file integrity.
    ``system`` is appended to the backend's system prompt. ``thinking`` is
    ``low``, ``medium`` or ``high``; OMP also accepts ``xhigh``. ``None`` leaves
    the backend's own default unchanged. OMP receives ``--thinking`` and Claude receives the SDK's
    ``effort`` option. These are backend-native settings, not calibrated levels
    across providers. ``omp_agent_dir``, ``omp_binary`` and ``omp_config_dir``
    are ``launch.build_session``'s omp profile, executable and config root,
    ``None`` for omp as installed. ``omp_session_dir`` is omp's ``--session-dir``
    in place of the run's own state (a hosted worker's usage directory, which
    the platform reads after the run; nothing removes it here).
    ``provider_session_id`` is an optional opaque OMP provider-affinity ID; it
    is passed to OMP's ``RpcClient`` and is not the run's telemetry
    ``session_id``. ``cache_retention`` optionally sets the OMP child process's
    ``PI_CACHE_RETENTION`` to ``short``, ``long`` or ``none``; ``None`` leaves
    OMP's own default unchanged. Neither option is supported by Claude.
    ``max_budget_usd`` is optional and Claude-only. When provided it is passed
    unchanged to the SDK's native query budget option, which can stop a query
    at its reported USD budget; it is not an invoice or exact-billing guarantee.
    OMP has no corresponding native request/run cap. Usage is observational.
    ``native_max_turns`` is optional and Claude-only. When set, the installed
    SDK's native ``max_turns`` option is consumed; this counts native model turns,
    not transport retries or invoices. ``RunResult`` exposes the configured cap,
    SDK capability and reported ``num_turns`` when the CLI supplies it.

    ``on_turn_result(result: RunResult) -> str | None`` optionally inspects each
    clean ordinary turn, including ``max_tokens`` and ``ended_by_tool``. It may
    be async; returning a non-empty string submits that text as the next user
    turn on the identical live session, preserving tools, conversation, runtime
    and telemetry ownership. Returning ``None`` stops. ``max_followups`` is a
    nonnegative integer, default 0: at most that many extra turns are submitted,
    even if the callback keeps asking. The callback still observes the final
    clean turn at the bound, but its requested correction is not submitted.
    Backend errors, rejections, cancellations, timeouts and native turn/budget
    limits never invoke it. Callback failures become an error result, never a
    retry. The whole-run timeout includes callbacks and all follow-up turns.
    The callback receives per-turn usage/calls/errors; the returned result sums
    usage and calls across turns (any unknown dimension stays unknown) and
    retains only the last turn's reply text and stop reason. Host code never
    replays a tool, successful or not.

    ``builtin_tools`` (omp only, default none) names omp built-in tools to leave
    enabled for this run, from ``BUILTIN_TOOLS`` (``web_search`` only);
    anything else is a ``ValueError``, and so is naming one on the Claude
    backend. The default is every built-in off. These tools run inside omp, so
    they bypass this module's permission broker, grading and ``grant`` entirely
    (omp runs ``--auto-approve``): they reach the network on the run's behalf with
    whatever arguments the model chooses, and what they return is untrusted text.
    They appear in ``RunResult.calls`` by omp's name. There is no hosted or
    interactive equivalent: ``create_app`` never enables them.

    ``usage_sink(event: dict) -> None`` (omp only) receives one event per
    provider request the run makes, with that request's own token counts
    (``hosted_state.usage_event``'s shape: ``event_id``, ``session_id``,
    ``conversation_id``, ``usage_epoch``, ``turn_index``, ``request_n``,
    ``backend``, nullable ``model``, ``provider``, ``provider_request_id``,
    ``cost_usd`` (nullable, an estimate reported by the provider message, not
    an invoice), ``cost_source`` when reported, nullable ``tokens_*``, ``ts``
    and ``usage_source``). ``event_id``
    is positional within the session/conversation/epoch, not a provider
    response id. ``session_id`` is minted for the run (``headless-<16
    hex>``), so two runs, concurrent or not, never share an id. It is
    telemetry only, to check the inference proxy's meter against: best effort,
    called from an executor thread (so thread-safe), idempotent by
    ``event_id``, and a sink that raises is logged and retried a few times,
    never failing or changing the run. The events are delivered before
    ``run_prompt`` returns (up to 10 seconds). No sink, no events, and the run
    is otherwise unchanged. The Claude backend gives no per-request figures,
    so it refuses a sink.

    ``on_runtime(event: AgentRuntimeResolved) -> None`` receives each runtime
    identity the backend actually reports, never the requested model. Startup
    events follow successful OMP state/model observation or SDK initialization;
    later reported changes may produce another event. Unknown fields remain
    null and ``provenance`` identifies the report source. For OMP role selection,
    ``requested_role``, ``resolved_role`` and ``role_provenance`` identify the
    alias requested, the alias OMP resolved to the reported model, and the
    verified OMP role-resolution path. Observer failures are logged and do not
    change the run.
    ``on_activity(event: dict) -> None`` receives ordered public ``text_delta``,
    ``tool_use`` (name/id/turn only) and ``tool_result`` (id/is_error only)
    records, including results arriving after a turn boundary. It receives no
    prompts, reasoning, tool arguments or tool result text. It is synchronous,
    best effort, and observer exceptions never change execution.
    ``on_owner(event)`` optionally receives native-process ownership and
    termination records for a Claude SDK run when ``owner_context`` supplies
    ``run_id``, ``project_id``, ``source`` and integer ``epoch``. The SDK
    identifies only its actual native CLI subprocess and checks its unique
    ``LOOM_CHILD_OWNER`` marker in ``/proc``; custom or unknown transports
    produce an ``unverified`` termination record instead. Observer failures
    are logged and do not change the run. Both arguments default to ``None``.

    Raises ``ValueError`` for a bad argument, ``CancelledError`` if cancelled
    (the session closed first); never for a model or backend failure.
    """
    _check_arguments(
        prompt,
        tools,
        backend,
        model,
        cwd,
        timeout,
        thinking,
        omp_request_timeout,
        provider_session_id,
        cache_retention,
        usage_sink,
        builtin_tools,
        on_runtime,
        max_budget_usd,
        native_max_turns,
        model_role,
        on_turn_result,
        max_followups,
    )
    for name, value in (("work_timeout", work_timeout), ("interrupt_timeout", interrupt_timeout)):
        if value is None and name == "work_timeout":
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError("%s must be a finite positive number of seconds" % name)
    if on_timeout is not None and not callable(on_timeout):
        raise ValueError("on_timeout must be a callable taking one TimeoutContext")
    if (isinstance(max_timeout_recoveries, bool) or not isinstance(max_timeout_recoveries, int)
            or max_timeout_recoveries < 0):
        raise ValueError("max_timeout_recoveries must be a nonnegative integer")
    if not isinstance(resume, bool):
        raise ValueError("resume must be a bool")
    if state_dir is not None:
        if not isinstance(state_dir, (str, os.PathLike)) or not os.fspath(state_dir):
            raise ValueError("state_dir must be a non-empty directory path")
        if backend != "omp":
            raise ValueError("durable headless state is only supported with backend='omp'")
    if resume and state_dir is None:
        raise ValueError("resume requires state_dir")
    started = asyncio.get_running_loop().time()

    with _conversation_state(state_dir, resume=resume, backend=backend, cwd=cwd) as state:
        bus = ViewerBus(ViewerRegistry(), url="")
        run = _Run(namespaced(product.current().mcp_server_name, ""),
                   on_runtime=on_runtime, on_activity=on_activity)
        def recovery_info():
            return RecoveryInfo(
                backend=backend,
                session_id=state.data["session_id"],
                state_dir=str(state.path) if state.durable else None,
                conversation_file=state.data["conversation_file"] if state.durable else None,
                resume_supported=backend == "omp" and state.durable,
                live_recovery_supported=backend == "omp",
            )

        def timeout_context(kind, interruption):
            elapsed = asyncio.get_running_loop().time() - started
            return TimeoutContext(
                kind=kind,
                result=(
                    run.turn_result() if kind == "rpc_request_timeout"
                    else run.turn_result(stop_reason="timeout", error="timeout")
                ),
                elapsed=elapsed,
                remaining=max(0, timeout - elapsed), interruption=interruption,
                recovery=recovery_info(),
                rpc_operation=getattr(run.session, "last_request_timeout", None),
            )

        if state.durable or work_timeout is not None or on_timeout is not None:
            run.recovery = recovery_info
        run.reserve_turn = state.reserve_turn
        server = ToolServer(
            [run.guard(t) for t in tools.tools],
            grading=tools.grading,
            bus=bus,
            paused_message=_PAUSED_MESSAGE,
        )
        # Claude asks under ``mcp__<server>__<tool>``; omp's host-tool gate under the bare name.
        claude_grants = tuple(namespaced(server.name, g) for g in tools.grant)
        broker = HeadlessBroker(run.on_event, granted=tuple(tools.grant) + claude_grants)
        # The channel a tool that reads its broker uses (app.py's, launch.py's).
        bus.tools = server
        bus.broker = broker
        # The per-request usage channel (app.py's, launch.py's): None unless the
        # caller gave a sink, and then omp registers nothing extra.
        usage = UsageRecorder(usage_sink)
        bus.request_usage = usage.report
        # Fresh runs have unique identities; explicit durable resumes retain
        # that identity and reserve new turn indices before any submission.
        session_id = state.data["session_id"]
        try:
            try:
                session = _build_session(
                    backend,
                    run.on_event,
                    broker=broker,
                    server=server,
                    allowed_tools=server.pre_allowed + claude_grants,
                    model=model,
                    model_role=model_role,
                    thinking=thinking,
                    cwd=cwd,
                    state_dir=str(state.path),
                    instructions=system or None,
                    omp_agent_dir=omp_agent_dir,
                    omp_session_dir=omp_session_dir,
                    omp_binary=omp_binary,
                    omp_config_dir=omp_config_dir,
                    omp_request_timeout=omp_request_timeout,
                    provider_session_id=provider_session_id,
                    cache_retention=cache_retention,
                    session_id=session_id,
                    on_request_usage=bus.request_usage,
                    builtin_tools=tuple(builtin_tools),
                    max_budget_usd=max_budget_usd,
                    native_max_turns=native_max_turns,
                    on_owner=on_owner,
                    owner_context=owner_context,
                    resume=state.data["conversation_file"] if resume else None,
                    resume_turn=state.data["turn"],
                    on_session_file=state.record_file,
                )
            except Exception as exc:
                # The backend's package missing (omp_rpc is installed apart), say.
                return run.result(stop_reason="error", error="%s: %s" % (type(exc).__name__, exc))
            run.session = session
            bus.end_turn_handler = getattr(session, "end_turn_after_tool", None)

            task = asyncio.ensure_future(
                _drive(
                    session, run, prompt, on_turn_result, max_followups,
                    work_timeout=work_timeout, interrupt_timeout=interrupt_timeout,
                    on_timeout=on_timeout, max_timeout_recoveries=max_timeout_recoveries,
                    timeout_context=timeout_context,
                    deadline=started + timeout,
                )
            )
            driver_cancelled = False
            try:
                remaining = max(0, timeout - (asyncio.get_running_loop().time() - started))
                await asyncio.wait({task}, timeout=remaining)
                if not task.done() or asyncio.get_running_loop().time() >= started + timeout:
                    # A known terminal backend failure is no longer active
                    # work. Bounded transport cleanup must not relabel it as
                    # a generation deadline or invoke timeout diagnostics.
                    if run.finished and run.error() and run.timeout_kind is None:
                        return run.result()
                    run.timeout_kind = "hard_deadline"
                    run.end()
                    broker.shutdown()
                    driver_cancelled = True
                    await _cancelled(task)
                    if on_timeout is not None and task.done():
                        observer = asyncio.ensure_future(
                            _timeout_callback(on_timeout, timeout_context("hard_deadline", "not_attempted"))
                        )
                        try:
                            await asyncio.wait({observer}, timeout=interrupt_timeout)
                            if observer.done():
                                with contextlib.suppress(Exception):
                                    observer.result()
                        finally:
                            await _cancelled(observer)
                    return run.result(stop_reason="timeout", error="timeout")
                await task
                return run.result()
            finally:
                # Also when cancelled: nothing outlives the call. Late tool calls
                # are refused first, then the backend is stopped.
                unfinished = not task.done()
                run.end()
                broker.shutdown()
                # A hard deadline already issued the driver's one cancellation.
                # Reissuing it would interrupt cancellation-cooperative cleanup
                # or a callback's deliberately bounded cancellation handler.
                if not driver_cancelled:
                    await _cancelled(task)
                await _close(
                    session, kill=unfinished or run.timeout_kind is not None or run.paused
                )
                if backend != "claude" and on_owner is not None and owner_context is not None:
                    try:
                        on_owner({"event": "native_termination", "outcome": "unverified"})
                    except Exception as exc:
                        sys.stderr.write("warning: native owner observer failed: %r\n" % (exc,))
                await run.drain()
        finally:
            run.end()
            # After the session is closed, so every request it reported is
            # queued; a caller that leaves the event loop next (asyncio.run
            # ending) would otherwise drop those still being delivered. Never
            # raises, bounded, and nothing at all without a sink.
            await usage.drain()
