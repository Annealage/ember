"""Headless attempts bind discovered remote definitions without sharing state."""

import asyncio

import pytest
import test_remote_tools as remote_tests
from test_headless import finish, make_args, script_session

from annealage_ember import remote
from annealage_ember.headless import ToolSet, run_prompt
from annealage_ember.remote import RemoteServer
from annealage_ember.tools import Grading

pytestmark = pytest.mark.asyncio

NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "object",
            "properties": {"part": {"type": "string"}},
            "required": ["part"],
            "additionalProperties": False,
        }
    },
    "required": ["query"],
    "additionalProperties": False,
}


@pytest.fixture
def upstream(monkeypatch):
    # The real discovery/proxy speaks MCP to the existing local fake server.
    monkeypatch.setattr(
        remote_tests,
        "ADVERTISED",
        [remote_tests._tool("lookup", "Find a part.", NESTED_SCHEMA)],
    )
    fake = remote_tests.FakeRemote()
    yield fake
    fake.stop()


def declaration(upstream):
    return RemoteServer(
        "fake",
        upstream.url,
        Grading(read=("lookup", "broken", "missing"), view=(), write=("store",)),
        prime=("begin", {}),
    )


def toolset(server, snapshot, observer):
    return ToolSet(
        tools=(),
        grading=Grading((), (), ()),
        remote=(server,),
        remote_snapshot=snapshot,
        remote_result_observer=observer,
    )


def definition(snapshot, name="lookup"):
    server = next(item for item in snapshot.servers if item.name == "fake")
    return next(item for item in server.tools if item.name == name)


async def test_discovery_availability_and_nested_schema_are_frozen(upstream, monkeypatch):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    assert snapshot.available("fake", "lookup")
    assert not snapshot.available("fake", "missing")
    assert not snapshot.available("fake", "extra")  # listed, but not graded
    assert not snapshot.available("unknown", "lookup")
    assert upstream.calls == ["begin"]  # discovery is not a lookup receipt

    schema = definition(snapshot).input_schema
    assert schema == NESTED_SCHEMA
    schema["properties"]["query"]["properties"]["part"]["type"] = "integer"
    assert definition(snapshot).input_schema == NESTED_SCHEMA

    same_snapshot = remote.discover((server,))
    assert same_snapshot.digest == snapshot.digest

    changed_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    monkeypatch.setattr(
        remote_tests,
        "ADVERTISED",
        [remote_tests._tool("lookup", "Find a part.", changed_schema)],
    )
    schema_snapshot = remote.discover((server,))
    assert definition(schema_snapshot).input_schema == changed_schema
    assert schema_snapshot.digest != snapshot.digest

    changed = {
        "type": "object",
        "properties": {"new_query": {"type": "string"}},
        "required": ["new_query"],
    }
    monkeypatch.setattr(
        remote_tests, "ADVERTISED", [remote_tests._tool("lookup", "Changed upstream.", changed)]
    )
    next_snapshot = remote.discover((server,))
    assert definition(snapshot).input_schema == NESTED_SCHEMA
    assert definition(snapshot).description == "Find a part."
    assert definition(next_snapshot).input_schema == changed
    assert definition(next_snapshot).description == "Changed upstream."


async def test_attempts_have_independent_guards_and_awaited_read_observers(
    upstream, monkeypatch, tmp_path
):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    receipts = [[], []]
    entered, release = asyncio.Event(), asyncio.Event()
    results = []
    handlers = []
    args = {"query": {"part": "RP2040"}}

    async def first_observer(*event):
        entered.set()
        await release.wait()
        receipts[0].append(event)

    async def second_observer(*event):
        receipts[1].append(event)

    async def attempt(observer, index):
        async def script(session):
            table = built.kwargs["server"].remote_tables()["fake"]
            assert table["lookup"].schema == NESTED_SCHEMA
            handlers.append(table["lookup"].handler)
            invalid = await table["lookup"].handler({"query": {"part": 42}})
            assert invalid["is_error"]
            assert upstream.calls.count("lookup") == index
            if index == 0:
                pending = asyncio.create_task(table["lookup"].handler(args))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    assert not pending.done()
                    assert receipts == [[], []]
                finally:
                    release.set()
                results.append(await pending)
            else:
                # The first run has closed; its guard cannot use the second run's authority.
                assert (await handlers[0](args))["is_error"]
                assert upstream.calls.count("lookup") == 1
                results.append(await table["lookup"].handler(args))
                assert (await table["broken"].handler({}))["is_error"]
                # Without a durable action ledger, a write fails before dispatch.
                denied = await table["store"].handler({"text": "note"})
                assert denied["is_error"] is True
                assert "durable action ledger is unavailable" in str(denied)
                assert len(receipts[1]) == 1
            await finish(session)

        built = script_session(monkeypatch, script)
        result = await run_prompt(
            "go", **make_args(tmp_path, tools=toolset(server, snapshot, observer))
        )
        assert result.error is None

    await attempt(first_observer, 0)
    assert len(receipts[0]) == 1 and receipts[1] == []
    await attempt(second_observer, 1)
    assert len(receipts[0]) == len(receipts[1]) == 1
    assert all(not result.get("is_error") for result in results)
    assert receipts[0][0] == ("fake", "lookup", args, results[0])
    assert receipts[1][0] == ("fake", "lookup", args, results[1])
    assert handlers[0] is not handlers[1]
    assert upstream.calls.count("lookup") == 2
    assert upstream.calls.count("broken") == 1
    assert upstream.calls.count("store") == 0
    assert upstream.stored == []


async def test_observer_mutation_cannot_change_delivered_result(upstream, monkeypatch, tmp_path):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    args = {"query": {"part": "RP2040"}}

    async def observer(_server, _tool, observed_args, observed_result):
        observed_args["query"]["part"] = "changed"
        observed_result["content"][0]["text"] = "observer replacement"
        observed_result["content"].append({"type": "text", "text": "observer secret"})

    async def script(session):
        handler = built.kwargs["server"].remote_tables()["fake"]["lookup"].handler
        result = await handler(args)
        assert not result.get("is_error")
        assert result["content"] == [{"type": "text", "text": "found {'part': 'RP2040'}"}]
        assert args == {"query": {"part": "RP2040"}}
        await finish(session)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, tools=toolset(server, snapshot, observer))
    )
    assert result.error is None
    assert upstream.calls.count("lookup") == 1


async def test_projector_output_is_what_the_model_and_the_observer_get(
    upstream, monkeypatch, tmp_path
):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    args = {"query": {"part": "RP2040"}}
    projected, receipts, delivered = [], [], []

    async def projector(server_name, tool_name, call_args, raw):
        projected.append((server_name, tool_name, call_args, raw["structured_content"]))
        return [{"type": "text", "text": "frozen"}]

    async def observer(*event):
        receipts.append(event)

    async def script(session):
        handler = built.kwargs["server"].remote_tables()["fake"]["lookup"].handler
        delivered.append(await handler(args))
        await finish(session)

    built = script_session(monkeypatch, script)
    tools = ToolSet(
        tools=(),
        grading=Grading((), (), ()),
        remote=(server,),
        remote_snapshot=snapshot,
        remote_result_observer=observer,
        remote_result_projector=projector,
    )
    result = await run_prompt("go", **make_args(tmp_path, tools=tools))
    assert result.error is None
    assert delivered == [{"content": [{"type": "text", "text": "frozen"}]}]
    assert receipts == [("fake", "lookup", args, delivered[0])]
    assert [entry[:3] for entry in projected] == [("fake", "lookup", args)]


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
async def test_observer_failure_fails_delivery(
    upstream, monkeypatch, tmp_path, caplog, capsys, error_type
):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    observed = []
    secret = "secret-receipt-identifier-7392"

    async def observer(*event):
        observed.append(event)
        raise error_type(secret)

    async def script(session):
        handler = built.kwargs["server"].remote_tables()["fake"]["lookup"].handler
        result = await handler({"query": {"part": "RP2040"}})
        assert result["is_error"]
        assert "found" not in str(result)
        assert secret not in str(result)
        await finish(session)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, tools=toolset(server, snapshot, observer))
    )
    assert result.error is None
    assert len(observed) == 1
    assert upstream.calls.count("lookup") == 1
    assert secret not in caplog.text
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


async def test_cancellation_during_observation_cannot_record(upstream, monkeypatch, tmp_path):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    entered = asyncio.Event()
    release = asyncio.Event()
    receipts = []

    async def observer(*event):
        entered.set()
        await release.wait()
        receipts.append(event)

    async def script(session):
        handler = built.kwargs["server"].remote_tables()["fake"]["lookup"].handler
        pending = asyncio.create_task(handler({"query": {"part": "RP2040"}}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            release.set()
        assert receipts == []
        await finish(session)

    built = script_session(monkeypatch, script)
    result = await run_prompt(
        "go", **make_args(tmp_path, tools=toolset(server, snapshot, observer))
    )
    assert result.error is None
    assert receipts == []
    assert upstream.calls.count("lookup") == 1


async def test_cancelled_run_cannot_call_or_observe_remote(upstream, monkeypatch, tmp_path):
    server = declaration(upstream)
    snapshot = remote.discover((server,))
    receipts = []
    ready = asyncio.Event()
    handlers = []

    async def observer(*event):
        receipts.append(event)

    async def script(session):
        handlers.append(built.kwargs["server"].remote_tables()["fake"]["lookup"].handler)
        ready.set()
        await asyncio.Event().wait()

    built = script_session(monkeypatch, script)
    task = asyncio.create_task(
        run_prompt("go", **make_args(tmp_path, tools=toolset(server, snapshot, observer)))
    )
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await handlers[0]({"query": {"part": "RP2040"}})
    assert result["is_error"]
    assert "lookup" not in upstream.calls
    assert receipts == []
