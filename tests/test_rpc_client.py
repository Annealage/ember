"""``LeanRpcClient``'s event history: bounded by what a reply is, not by how it streamed,
with every contract ``RpcClient``'s ``collect_events`` / ``prompt_and_wait`` readers rely on kept."""

import gc
import threading
import tracemalloc
import weakref

import pytest

pytest.importorskip("omp_rpc")

from omp_rpc import (
    RpcClient,  # noqa: E402
    RpcError,  # noqa: E402
)
from omp_rpc.protocol import MessageEndEvent, parse_notification  # noqa: E402

from annealage_ember import observability  # noqa: E402
from annealage_ember.session.rpc_client import (  # noqa: E402
    LISTENER_ATTRS,
    LeanRpcClient,
    estimated_size,
)

BASE = {
    "role": "assistant",
    "api": "a",
    "provider": "p",
    "model": "m",
    "stopReason": "stop",
    "timestamp": 1,
}


def _update(text, delta="x"):
    part = {**BASE, "content": [{"type": "text", "text": text}]}
    return {
        "type": "message_update",
        "message": part,
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": delta,
            "partial": part,
        },
    }


def _stream(n, chunk=16):
    text = ""
    for _ in range(n):
        text += "x" * chunk
        yield _update(text, "x" * chunk)


def _retained(client_class, n):
    client = client_class(max_event_history=10_000)
    tracemalloc.start()
    for payload in _stream(n):
        client._append_event(payload)
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return current


def test_a_long_reply_is_not_retained_once_per_token():
    lean, stock = _retained(LeanRpcClient, 2000), _retained(RpcClient, 2000)
    assert lean < stock / 10, (lean, stock)


def test_a_streamed_update_is_counted_and_heard_by_listeners_but_not_archived():
    client = LeanRpcClient()
    client._append_event(_update("abc", "c"))
    client._append_event({"type": "agent_start"})
    assert [e["type"] for e in client._events.items] == ["agent_start"]


def test_every_other_notification_is_archived_as_a_clone_equal_to_upstreams():
    end = {
        "type": "message_end",
        "message": {**BASE, "content": [{"type": "text", "text": "done"}]},
    }
    lean, stock = LeanRpcClient(), RpcClient()
    lean._append_event(end)
    stock._append_event(end)
    assert lean._events.items == stock._events.items and lean._events.items[0] is not end


def test_a_turn_of_more_than_the_history_limit_in_updates_still_completes_where_upstream_cannot():
    events, final = _turn_events()
    long_turn = [events[0], *_stream(10_500, chunk=1), *events[-2:]]

    def run(client):
        out = {}

        def wait():
            try:
                out["events"] = client.collect_events(timeout=10)
            except RpcError as exc:
                out["error"] = exc

        waiter = threading.Thread(target=wait)
        waiter.start()
        while not client._prompt_lifecycle.active_operation:
            pass
        for payload in long_turn:
            client._append_event(payload)
        waiter.join(15)
        return out

    assert "Event history limit" in str(run(RpcClient()).get("error"))
    got = run(LeanRpcClient())["events"]
    assert isinstance(got[-1], parse_notification(long_turn[-1]).__class__)
    assert [type(e).__name__ for e in got].count("MessageEndEvent") == 1


def test_a_listener_still_receives_every_update_with_its_full_content():
    client = LeanRpcClient()
    seen = []
    client.on_message_update(seen.append)
    for payload in _stream(3):
        notification = parse_notification(payload)
        client._dispatch_listeners(
            "typed_event",
            "message_update",
            client._typed_event_listeners["message_update"],
            notification,
        )
        client._append_event(payload)
    assert [e.message["content"][0]["text"] for e in seen] == ["x" * 16, "x" * 32, "x" * 48]


def _turn_events(*, compact=False):
    tool = {
        "type": "toolCall",
        "id": "t1",
        "name": "write",
        "arguments": {"path": "a", "body": "z" * 500},
    }
    final = {**BASE, "content": [{"type": "text", "text": "hello"}, tool]}
    messages = [] if compact else [final]
    events = [{"type": "agent_start"}]
    events += list(_stream(50))
    events.append({"type": "message_end", "message": final})
    events.append({"type": "agent_end", "messages": messages, "messageCount": 1})
    return events, final


@pytest.mark.parametrize("compact", [False, True])
def test_collect_events_rebuilds_the_final_messages_and_tool_arguments(compact):
    client = LeanRpcClient()
    events, final = _turn_events(compact=compact)
    result = {}

    def wait():
        result["events"] = client.collect_events(timeout=5)

    waiter = threading.Thread(target=wait)
    waiter.start()
    while not client._prompt_lifecycle.active_operation:
        pass
    for payload in events:
        client._append_event(payload)
    waiter.join(5)
    got = result["events"]
    ends = [e for e in got if isinstance(e, MessageEndEvent)]
    assert (
        len(ends) == 1
        and ends[0].message["content"][1]["arguments"] == final["content"][1]["arguments"]
    )
    terminal = got[-1]
    assert (
        LeanRpcClient._complete_agent_end_messages(got, terminal)[-1]["content"][1]["arguments"]
        == final["content"][1]["arguments"]
    )


def test_the_history_limit_is_upstreams_and_trimming_keeps_the_size_estimate_honest():
    client = LeanRpcClient(max_event_history=100)
    ends = [
        {"type": "message_end", "message": {**BASE, "content": [{"type": "text", "text": "é" * i}]}}
        for i in range(250)
    ]
    assert RpcClient().__dict__["_max_event_history"] == 10_000
    assert LeanRpcClient().__dict__["_max_event_history"] == 10_000
    for payload in ends:
        client._append_event(payload)
    snapshot = client.metrics_snapshot()
    assert snapshot["retained_events"] == 100
    assert client._events.offset == 150
    assert snapshot["retained_estimated_bytes"] == sum(client._events.sizes)
    assert estimated_size(ends[249]) > len(
        str(ends[249]).encode("ascii", "ignore")
    )  # UTF-8 bytes, not code points
    assert client._events.sizes[-1] == estimated_size(ends[249])


def test_an_archived_event_is_not_the_notification_object_a_reader_may_mutate():
    client = LeanRpcClient()
    payload = {
        "type": "agent_end",
        "messages": [{"role": "user", "content": "q"}],
        "messageCount": 1,
    }
    client._append_event(payload)
    payload["messages"][0]["content"] = "mutated"
    assert client._events.items[0]["messages"][0]["content"] == "q"


def test_stop_releases_the_archive_and_the_listeners():
    client = LeanRpcClient()
    client.on_event(lambda e: None)
    client.on_message_update(lambda e: None)
    client._append_event({"type": "agent_start"})
    assert (
        client.metrics_snapshot()["retained_events"] == 1
        and client.metrics_snapshot()["listeners"] >= 2
    )
    client.stop()  # never started: nothing to stop, but what it held still goes
    snapshot = client.metrics_snapshot()
    assert (
        snapshot["retained_events"],
        snapshot["retained_estimated_bytes"],
        snapshot["listeners"],
    ) == (0, 0, 0)
    assert all(not getattr(client, name) for name in LISTENER_ATTRS)


def test_a_client_that_goes_away_leaves_the_aggregate_gauges():
    client = LeanRpcClient()
    client._append_event({"type": "agent_start"})
    ref = weakref.ref(client)
    assert client in observability._tracked_clients()
    del client
    gc.collect()
    assert ref() is None and ref not in [weakref.ref(c) for c in observability._tracked_clients()]


def test_a_client_that_cannot_be_read_removes_the_aggregates_instead_of_lowering_them():
    class Broken:
        def metrics_snapshot(self):
            raise RuntimeError("unreadable")

    broken = Broken()
    observability.track_client(broken)
    families = {f.name: f for f in observability._RpcCollector().collect()}
    assert families["annealage_omp_metrics_unreadable"].samples[0].value >= 1
    assert "annealage_omp_sessions" not in families
    assert "annealage_omp_rpc_retained_events" not in families
