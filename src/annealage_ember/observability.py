"""One process-wide metrics registry, a protected ``/metrics`` exporter, and what Ember itself measures.

The contract (Loom, Mesh, Trace and Canvas all use this one module; nothing makes a
second registry, a second process collector or a second ``/metrics``):

``registry()``
    The process's one ``prometheus_client.CollectorRegistry`` (not the library's global
    default), created on first use with ``ProcessCollector`` (CPU, resident and virtual
    memory, open fds, start time), ``GCCollector`` and ``ThreadCollector`` (live Python
    threads: the library's process collector has no thread count).
``counter`` / ``gauge`` / ``histogram``
    Get-or-create by name. The same name asked for again with the same type, help text,
    label names (and buckets) returns the same metric; any difference raises
    ``ValueError``, so two modules cannot quietly share, or fight over, one name.
    Names start with ``annealage_``. A label may not be named like an identity, path
    or payload (``FORBIDDEN_LABELS``), and a label's values are the caller's to keep
    to a small fixed set: ``bounded()`` clamps a value to an allowlist with an
    ``other`` fallback, which is how the labels here are built.
``add_collector``
    Register a custom collector (an object with ``collect()``) once; a second call with
    the same object is a no-op. Gauges that are a function of live state belong here,
    computed at scrape time, not on a timer.
``render()``
    ``(body, content_type)`` for the registry.
``mount(app, *, auth, metrics_token=None)`` / ``create_app(..., metrics=True)``
    ``GET /metrics`` on ``app``, once (a second call on the same app is a no-op), with
    request timing and the loop-lag sampler. ``create_app`` calls it unless
    ``metrics=False``; a ``FrontDoor`` mounts it on its own root and its mounted apps
    do not. Never the agent token. The credential is ``metrics_token`` (default: the
    ``ANNEALAGE_METRICS_TOKEN`` environment variable; at least 32 characters, a 256-bit
    random secret is the intent): a scraper sends ``Authorization: Bearer <token>``,
    compared in constant time, and when a token is configured it is the ONLY way in (no
    browser or tailnet login opens ``/metrics``). It opens nothing else. Without one,
    the app's browser/tailnet authentication, as the agent logs use (in hosted mode,
    ``project.admin``). A refusal is ``403`` with the body ``forbidden``, as everywhere.

What Ember measures (all aggregate; no session, project, path, model or user label):

``annealage_http_request_seconds{method,route,status_class}``
    Route is the registered URL template, or ``unmatched``; a path no route matches is counted in ``annealage_http_unmatched_total{method,status_class}`` (no duration: microdot runs no hooks before a 404); method is one of the
    standard verbs or ``other``; status_class is ``1xx``..``5xx``.
``annealage_http_requests_inflight``
``annealage_event_loop_lag_seconds`` (histogram) and ``annealage_event_loop_lag_current_seconds``
    How late a 0.5 s ``call_later`` fires, from one task per running loop.
``annealage_websocket_connections``
    Open page WebSockets (``/ws``); the per-connection detail is deliberately not a label.
``annealage_omp_sessions``, ``annealage_omp_child_rss_bytes``, ``annealage_omp_child_cpu_seconds``
    Live ``LeanRpcClient`` count (a running child), and the resident memory and CPU time so
    far of those omp child processes (``/proc/<pid>``; their descendants are the product's
    process-tree sampler's). A gauge over the live set, not a counter. A value that cannot be
    read is omitted and ``annealage_omp_metrics_unreadable`` says how many clients failed.
    NOT measured: bytes received from omp. omp-rpc's reader decodes each line inside a
    method that gives a subclass no hook for its length, and reimplementing the reader is
    declined; ``retained_estimated_bytes`` is the encoded size of what the history keeps.
``annealage_omp_rpc_events_total{type}``
    Notifications archived, ``type`` from the protocol's fixed list or ``other``.
``annealage_omp_rpc_retained_events``, ``annealage_omp_rpc_retained_estimated_bytes``
    Summed over live clients. ``estimated`` means the length of the JSON of what the
    history holds, not the Python objects'.
``annealage_omp_rpc_listeners``, ``annealage_omp_rpc_pending_requests``,
``annealage_omp_rpc_pending_host_calls``
    Summed over live clients; a number that does not fall when sessions end is a leak.
``annealage_omp_turns_total{outcome}`` (``completed``/``cancelled``/``failed``) and
``annealage_omp_turn_seconds``
``annealage_omp_session_stops_total``
"""

from __future__ import annotations

import asyncio
import hmac
import os
import threading
import time
import weakref
from typing import Iterable, Optional

from prometheus_client import CollectorRegistry, GCCollector, ProcessCollector
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.exposition import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.metrics import Counter, Gauge, Histogram

#: Label names no metric may use: they would be identities, paths or payloads.
FORBIDDEN_LABELS = frozenset(
    {
        "id",
        "session",
        "session_id",
        "project",
        "project_id",
        "path",
        "file",
        "url",
        "model",
        "user",
        "name",
        "token",
        "payload",
        "prompt",
        "pid",
    }
)

#: The protocol's notification types worth a series of their own; anything else is ``other``.
RPC_EVENT_TYPES = (
    "agent_start",
    "agent_end",
    "turn_start",
    "turn_end",
    "message_start",
    "message_update",
    "message_end",
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "auto_compaction_start",
    "auto_compaction_end",
    "auto_retry_start",
    "auto_retry_end",
    "todo_reminder",
    "todo_auto_clear",
)
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
TURN_OUTCOMES = ("completed", "cancelled", "failed")

#: Every metric name starts with this (built, not spelled out, in this file's code).
PREFIX = "annealage" + "_"
_lock = threading.RLock()
_registry: Optional[CollectorRegistry] = None
_definitions: dict = {}
_collectors: "weakref.WeakSet" = weakref.WeakSet()


def bounded(value, allowed: Iterable[str]) -> str:
    """``value`` when it is one of ``allowed``, else ``other``."""
    return value if value in allowed else "other"


def registry() -> CollectorRegistry:
    global _registry
    with _lock:
        if _registry is None:
            _registry = CollectorRegistry(auto_describe=True)
            ProcessCollector(registry=_registry)
            GCCollector(registry=_registry)
            _registry.register(ThreadCollector())
        return _registry


def _define(kind, cls, name, documentation, labels, **extra):
    labels = tuple(labels)
    if not name.startswith("annealage_"):
        raise ValueError("metric names start with annealage_: %r" % (name,))
    bad = FORBIDDEN_LABELS.intersection(labels)
    if bad:
        raise ValueError(
            "%s: labels %s would be identities, paths or payloads" % (name, sorted(bad))
        )
    key = (kind, documentation, labels, tuple(sorted(extra.items())))
    with _lock:
        known = _definitions.get(name)
        if known is not None:
            if known[0] != key:
                raise ValueError(
                    "%s is already defined differently: %r vs %r" % (name, known[0], key)
                )
            return known[1]
        metric = cls(name, documentation, labels, registry=registry(), **extra)
        _definitions[name] = (key, metric)
        return metric


def counter(name: str, documentation: str, labels: Iterable[str] = ()) -> Counter:
    return _define("counter", Counter, name, documentation, labels)


def gauge(name: str, documentation: str, labels: Iterable[str] = ()) -> Gauge:
    return _define("gauge", Gauge, name, documentation, labels)


def histogram(
    name: str, documentation: str, labels: Iterable[str] = (), buckets=Histogram.DEFAULT_BUCKETS
) -> Histogram:
    return _define("histogram", Histogram, name, documentation, labels, buckets=tuple(buckets))


def add_collector(collector) -> None:
    """Register ``collector`` on ``registry()`` once."""
    with _lock:
        if collector not in _collectors:
            registry().register(collector)
            _collectors.add(collector)


def render():
    return generate_latest(registry()), CONTENT_TYPE_LATEST


class ThreadCollector:
    """``annealage_process_threads``: live Python threads, and OS threads where ``/proc`` says."""

    def collect(self):
        family = GaugeMetricFamily(PREFIX + "process_threads", "Live threads", labels=("kind",))
        family.add_metric(("python",), threading.active_count())
        os_threads = _proc_threads()
        if os_threads is not None:
            family.add_metric(("os",), os_threads)
        yield family


def _proc_threads() -> Optional[int]:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# omp RPC / lifecycle
# ---------------------------------------------------------------------------

_clients: "weakref.WeakSet" = weakref.WeakSet()
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def track_client(client) -> None:
    """Count ``client`` (anything with ``metrics_snapshot()``) in the aggregate gauges until it is collected.
    Clients come and go on any thread; the set is only touched under the module lock."""
    with _lock:
        _clients.add(client)
    _ensure_rpc_metrics()


def _tracked_clients():
    with _lock:
        return list(_clients)


def rpc_event(event_type) -> None:
    _ensure_rpc_metrics()
    _rpc_events.labels(bounded(event_type, RPC_EVENT_TYPES)).inc()


def turn_finished(outcome: str, seconds: float) -> None:
    _ensure_rpc_metrics()
    _turns.labels(bounded(outcome, TURN_OUTCOMES)).inc()
    _turn_seconds.observe(seconds)


def session_stopped() -> None:
    _ensure_rpc_metrics()
    _stops.inc()


def websocket_connections():
    """The gauge of open page WebSockets (``http/ws.py`` raises and lowers it)."""
    return gauge(PREFIX + "websocket_connections", "Open page WebSocket connections")


def _child_usage(pid):
    """``(resident bytes, cpu seconds)`` of ``pid`` from ``/proc``, or ``None`` when it cannot be read."""
    try:
        with open("/proc/%d/statm" % pid) as f:
            rss = int(f.read().split()[1]) * _PAGE
        with open("/proc/%d/stat" % pid) as f:
            # The command is parenthesised and may hold spaces; fields count from after it.
            fields = f.read().rsplit(")", 1)[1].split()
        return rss, (int(fields[11]) + int(fields[12])) / _TICKS
    except (OSError, ValueError, IndexError):
        return None


class _RpcCollector:
    """Aggregates over the live omp clients, computed at scrape time.

    A value that cannot be read is omitted, never reported as zero: a client whose
    snapshot fails removes the aggregates it feeds and raises
    ``annealage_omp_metrics_unreadable`` (the number of clients that could not be
    read), and an omp child whose ``/proc`` entry cannot be read does the same to the
    two child gauges. ``child_cpu_seconds`` is a gauge over the live set: it falls when
    a child ends (it is not a counter)."""

    def collect(self):
        snapshots, unreadable = [], 0
        for client in _tracked_clients():
            try:
                snapshots.append(client.metrics_snapshot())
            except Exception:
                unreadable += 1
        running = [s for s in snapshots if s["pid"]]
        usage = [_child_usage(s["pid"]) for s in running]
        children_readable = all(u is not None for u in usage)

        def one(name, doc, value):
            family = GaugeMetricFamily(PREFIX + name, doc)
            family.add_metric((), value)
            return family

        yield one(
            "omp_metrics_unreadable", "Live omp clients whose state could not be read", unreadable
        )
        if unreadable:
            return
        yield one("omp_sessions", "Live omp RPC clients (a running child)", len(running))
        if children_readable:
            yield one(
                "omp_child_rss_bytes",
                "Resident memory of the live omp children",
                sum(u[0] for u in usage),
            )
            yield one(
                "omp_child_cpu_seconds",
                "CPU time so far of the live omp children (a gauge over the live set)",
                sum(u[1] for u in usage),
            )
        total = lambda key: sum(s[key] for s in snapshots)  # noqa: E731
        yield one(
            "omp_rpc_retained_events", "Events held in client histories", total("retained_events")
        )
        yield one(
            "omp_rpc_retained_estimated_bytes",
            "Estimated encoded JSON size of the events held in client histories",
            total("retained_estimated_bytes"),
        )
        yield one("omp_rpc_listeners", "Registered client listeners", total("listeners"))
        yield one(
            "omp_rpc_pending_requests", "Requests awaiting a response", total("pending_requests")
        )
        yield one(
            "omp_rpc_pending_host_calls",
            "Host tool/URI calls in flight",
            total("pending_host_calls"),
        )


_rpc_ready = False


def _ensure_rpc_metrics() -> None:
    global _rpc_ready, _rpc_events, _turns, _turn_seconds, _stops
    if _rpc_ready:
        return
    with _lock:
        if _rpc_ready:
            return
        _rpc_events = counter(
            PREFIX + "omp_rpc_events_total", "RPC notifications archived", ("type",)
        )
        _turns = counter(PREFIX + "omp_turns_total", "Turns finished", ("outcome",))
        _turn_seconds = histogram(
            PREFIX + "omp_turn_seconds",
            "Turn duration",
            buckets=(1, 5, 15, 60, 180, 600, 1800, 3600),
        )
        _stops = counter(PREFIX + "omp_session_stops_total", "omp sessions stopped")
        add_collector(_RpcCollector())
        _rpc_ready = True


# ---------------------------------------------------------------------------
# HTTP and loop lag
# ---------------------------------------------------------------------------

LAG_TICK = 0.5
_http = None
_loop_monitors: dict = {}


def _http_metrics():
    global _http
    with _lock:
        if _http is None:
            _http = (
                histogram(
                    PREFIX + "http_request_seconds",
                    "HTTP request duration",
                    ("method", "route", "status_class"),
                    buckets=(0.005, 0.025, 0.1, 0.5, 2, 10, 60),
                ),
                gauge(PREFIX + "http_requests_inflight", "HTTP requests being handled"),
                histogram(
                    PREFIX + "event_loop_lag_seconds",
                    "How late the event loop ran a 0.5 s timer",
                    buckets=(0.001, 0.005, 0.025, 0.1, 0.5, 2, 10),
                ),
                gauge(
                    PREFIX + "event_loop_lag_current_seconds", "The latest event loop lag sample"
                ),
            )
        return _http


async def _monitor_loop(lag, current):
    loop = asyncio.get_running_loop()
    while True:
        due = loop.time() + LAG_TICK
        await asyncio.sleep(LAG_TICK)
        late = max(0.0, loop.time() - due)
        lag.observe(late)
        current.set(late)


def start_loop_monitor() -> None:
    """Sample this running loop's lag; once per loop, from any number of apps."""
    loop = asyncio.get_running_loop()
    if loop in _loop_monitors:
        return
    for other in [lp for lp in _loop_monitors if lp.is_closed()]:
        _loop_monitors.pop(other)
    _, _, lag, current = _http_metrics()
    _loop_monitors[loop] = loop.create_task(_monitor_loop(lag, current))


def stop_loop_monitor() -> None:
    """Stop this loop's sampler (``serve`` calls it on the way out)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = _loop_monitors.pop(loop, None)
    if task is not None:
        task.cancel()
    for other in [lp for lp in _loop_monitors if lp.is_closed()]:
        _loop_monitors.pop(other).cancel()


def _templates(app):
    """``{handler: template}`` over the app's routes and its mounted apps'."""
    table = {}
    for _methods, pattern, handler, _prefix, subapp in app.url_map:
        # Never the mount prefix: it holds an app's id.
        table.setdefault(handler, pattern.url_pattern)
        if subapp is not None:
            table.update({h: t for h, t in _templates(subapp).items() if h not in table})
    return table


METRICS_TOKEN_ENV = "ANNEALAGE_METRICS_TOKEN"
MIN_TOKEN_LENGTH = 32


def mount(app, *, auth, metrics_token: Optional[str] = None) -> None:
    """Add ``GET /metrics`` and request timing to ``app``, once."""
    if getattr(app, "agent_metrics_mounted", False):
        return
    metrics_token = metrics_token or os.environ.get(METRICS_TOKEN_ENV) or None
    if metrics_token is not None and len(metrics_token) < MIN_TOKEN_LENGTH:
        raise ValueError("the metrics token must be at least %d characters" % MIN_TOKEN_LENGTH)
    app.agent_metrics_mounted = True
    unmatched = counter(
        PREFIX + "http_unmatched_total", "Requests no route matched", ("method", "status_class")
    )
    request_seconds, inflight, _, _ = _http_metrics()
    cache = {"size": -1, "table": {}}

    def route_of(req):
        handler = getattr(req, "route", None)
        if not callable(handler):
            return "unmatched"
        if cache["size"] != len(app.url_map):
            cache["table"], cache["size"] = _templates(app), len(app.url_map)
        return cache["table"].get(handler, "unmatched")

    @app.before_request
    async def _begin(req):
        start_loop_monitor()
        if getattr(req.g, "obs_started", None) is not None:
            return  # an outer app is timing this request already
        req.g.obs_started = time.perf_counter()
        inflight.inc()

    async def _end(req, res):
        started = getattr(getattr(req, "g", None), "obs_started", None) if req is not None else None
        if started is None and req is not None and not callable(getattr(req, "route", None)):
            # microdot runs no before_request handler for a path no route matches,
            # so there is no start time: a count, not a duration.
            unmatched.labels(
                bounded(req.method.upper(), HTTP_METHODS), "%dxx" % (res.status_code // 100)
            ).inc()
        if started is not None:
            inflight.dec()
            req.g.obs_started = None
            request_seconds.labels(
                bounded(req.method.upper(), HTTP_METHODS),
                route_of(req),
                "%dxx" % (res.status_code // 100),
            ).observe(time.perf_counter() - started)
        return res

    app.after_request(_end)
    app.after_error_request(_end)

    def allowed(req):
        header = req.headers.get("Authorization", "")
        if metrics_token:
            if not header.startswith("Bearer "):
                return False
            return hmac.compare_digest(header[7:].encode(), metrics_token.encode())
        return (
            auth.authenticate(req, "project.admin" if auth.hosted_mode else "project.read")
            is not None
        )

    @app.get("/metrics")
    async def metrics(req):
        if not allowed(req):
            from .http.ws import refusal

            return refusal()
        loop = asyncio.get_running_loop()
        body, content_type = await loop.run_in_executor(None, render)
        from microdot import Response

        return Response(body=body, headers={"Content-Type": content_type})

    app.agent_metrics_handler = metrics


def unmount_route(app) -> None:
    """Take ``/metrics`` back out of ``app`` (a ``FrontDoor`` serves the one for the process)."""
    handler = getattr(app, "agent_metrics_handler", None)
    if handler is not None:
        app.url_map[:] = [entry for entry in app.url_map if entry[2] is not handler]
