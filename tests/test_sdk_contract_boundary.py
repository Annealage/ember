"""Exercise the Claude SDK's real tools/call boundary without a provider."""

import asyncio
import json
from types import SimpleNamespace

import jsonschema
import pytest
from claude_agent_sdk import tool
from conftest import mcp_client

from annealage_ember.tools import Grading, ToolServer, fail, ok

pytestmark = pytest.mark.asyncio

SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "mode": {"enum": ["read", "write"]},
    },
    "required": ["count", "mode"],
    "additionalProperties": False,
}


def server_for(handler):
    registered = tool("inspect", "Inspect an item.", SCHEMA)(handler)
    return ToolServer(
        [registered],
        grading=Grading(read=("inspect",), view=(), write=()),
        bus=SimpleNamespace(paused=False),
        paused_message="paused",
    ).server["instance"]


def text(result):
    return "\n".join(block.text for block in result.content if block.type == "text")


@pytest.mark.parametrize("field", ["count", "mode"])
async def test_sdk_native_validation_rejects_without_echoing_values(field, monkeypatch):
    calls = []
    validations = []
    validate = jsonschema.validate

    def observe_native_validation(*args, **kwargs):
        try:
            validate(*args, **kwargs)
        except jsonschema.ValidationError:
            validations.append("rejected")
            raise
        validations.append("accepted")

    monkeypatch.setattr(jsonschema, "validate", observe_native_validation)

    async def handler(args):
        calls.append(args)
        return ok({"accepted": True})

    secret = "candidate-private-material-" + "z" * 20000
    arguments = {"count": 1, "mode": "read", field: secret}
    async with mcp_client(server_for(handler)) as client:
        result = await client.call_tool("inspect", arguments)

    assert validations == ["rejected"]
    assert calls == []
    assert result.is_error is True
    rendered = text(result)
    assert "candidate-private-material-" not in rendered
    assert len(rendered) < 20000
    diagnostic = json.loads(rendered)
    assert diagnostic["code"] == "invalid_input"
    assert diagnostic["issues"]


async def test_valid_input_and_handler_failure_keep_native_results():
    calls = []

    async def handler(args):
        calls.append(args)
        if args["mode"] == "write":
            return fail("Input validation error: handler's own failure")
        return ok({"accepted": args["count"]})

    async with mcp_client(server_for(handler)) as client:
        success = await client.call_tool("inspect", {"count": 7, "mode": "read"})
        failure = await client.call_tool("inspect", {"count": 8, "mode": "write"})

    assert calls == [{"count": 7, "mode": "read"}, {"count": 8, "mode": "write"}]
    assert success.is_error is False
    assert json.loads(text(success)) == {"accepted": 7}
    assert failure.is_error is True
    assert text(failure) == "Input validation error: handler's own failure"


async def test_handler_exception_is_preserved_and_unknown_name_is_not_echoed():
    async def handler(args):
        raise ValueError("Input validation error: handler rejected the operation")

    unknown_name = "unknown-private-material-" + "x" * 20000
    async with mcp_client(server_for(handler)) as client:
        failure = await client.call_tool("inspect", {"count": 1, "mode": "read"})
        unknown = await client.call_tool(unknown_name, {})

    assert failure.is_error is True
    assert text(failure) == "Input validation error: handler rejected the operation"
    assert unknown.is_error is True
    assert "unknown-private-material-" not in text(unknown)
    assert len(text(unknown)) < 20000


async def test_concurrent_rejection_does_not_change_an_entered_handler_failure():
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def handler(args):
        calls.append(args)
        entered.set()
        await release.wait()
        return fail("Input validation error: concurrent handler failure")

    async with mcp_client(server_for(handler)) as client:
        running = asyncio.create_task(client.call_tool("inspect", {"count": 1, "mode": "read"}))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            rejected = await asyncio.wait_for(
                client.call_tool(
                    "inspect", {"count": "concurrent-private-material", "mode": "read"}
                ),
                timeout=5,
            )
        finally:
            release.set()
            completed = await asyncio.wait_for(running, timeout=5)

    assert calls == [{"count": 1, "mode": "read"}]
    assert rejected.is_error is True
    assert "concurrent-private-material" not in text(rejected)
    assert json.loads(text(rejected))["code"] == "invalid_input"
    assert completed.is_error is True
    assert text(completed) == "Input validation error: concurrent handler failure"


async def test_remote_sdk_server_rejects_before_proxying(tmp_path):
    # Reuse the suite's real local MCP remote and its discovery/priming setup.
    from test_remote_tools import FakeRemote, _fake, _tools

    remote = FakeRemote()
    try:
        tools = _tools(SimpleNamespace(paused=False), tmp_path, _fake(remote))
        assert len(tools.remotes) == 1
        before = list(remote.calls)
        secret = "remote-private-material-" + "x" * 20000
        async with mcp_client(tools.remotes[0].server["instance"]) as client:
            rejected = await client.call_tool("lookup", {"query": [secret]})
            accepted = await client.call_tool("lookup", {"query": "RP2040"})
        assert rejected.is_error is True
        assert "remote-private-material-" not in text(rejected)
        assert len(text(rejected)) < 20000
        assert json.loads(text(rejected))["code"] == "invalid_input"
        assert accepted.is_error is False
        assert text(accepted) == "found RP2040"
        assert remote.calls == before + ["lookup"]
    finally:
        remote.stop()
