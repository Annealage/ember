"""The per-call recorder observes the real tool boundaries, not tool bodies."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import jsonschema
import pytest
from claude_agent_sdk import tool
from conftest import mcp_client
from test_remote_tools import FakeRemote, _fake

from annealage_ember.tools import Grading, ToolServer, ok

pytestmark = pytest.mark.asyncio

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def field(event, name):
    return event[name] if isinstance(event, dict) else getattr(event, name)


def local(handler, recorder, *, bus=None, grade="read", remote=()):
    registered = tool("inspect", "Inspect an item.", SCHEMA)(handler)
    grades = {"read": (), "view": (), "write": ()}
    grades[grade] = ("inspect",)
    return ToolServer(
        [registered],
        grading=Grading(**grades),
        bus=bus or SimpleNamespace(paused=False),
        paused_message="paused",
        remote=remote,
        call_recorder=recorder,
    )


def assert_event(event, server, name, outcome, stage=None):
    assert field(event, "server") == server
    assert field(event, "tool") == name
    assert field(event, "outcome") == outcome
    if stage is not None:
        assert field(event, "stage") == stage


def assert_redacted(events, *secrets):
    rendered = repr(events)
    assert all(secret not in rendered for secret in secrets)
    assert len(rendered) < 8192


async def test_sdk_native_rejection_and_success_are_each_recorded_once(monkeypatch):
    calls, events, validations = [], [], []
    validate = jsonschema.validate

    def observe(*args, **kwargs):
        try:
            return validate(*args, **kwargs)
        except jsonschema.ValidationError:
            validations.append("native rejection")
            raise

    monkeypatch.setattr(jsonschema, "validate", observe)

    async def handler(args):
        calls.append(args.copy())
        return ok({"answer": "result-private-value"})

    server = local(handler, events.append)
    secret = "candidate-private-value-" + "x" * 20000
    async with mcp_client(server.server["instance"]) as client:
        rejected = await client.call_tool("inspect", {"query": [secret]})
        accepted = await client.call_tool("inspect", {"query": "argument-private-value"})

    assert validations == ["native rejection"]
    assert rejected.is_error and not accepted.is_error
    assert calls == [{"query": "argument-private-value"}]
    assert len(events) == 2
    assert_event(events[0], server.name, "inspect", "rejected", "sdk")
    assert_event(events[1], server.name, "inspect", "success", "handler")
    assert_redacted(
        events, "candidate-private-value", "argument-private-value", "result-private-value"
    )


async def test_schema_pause_and_hosted_reject_before_handler():
    calls, events = [], []

    async def handler(args):
        calls.append(args)
        return ok({"ok": True})

    bus = SimpleNamespace(paused=False)
    server = local(handler, events.append, bus=bus, grade="write")
    invoke = server.tool_table()["inspect"].handler
    invalid = await invoke({"query": 12})
    bus.paused = True
    paused = await invoke({"query": "pause-private"})
    bus.paused = False
    bus.hosted_mode = True
    bus.hosted_tool_ops = {}
    hosted = await invoke({"query": "hosted-private"})

    assert all(result["is_error"] for result in (invalid, paused, hosted))
    assert calls == []
    assert len(events) == 3
    for event, stage in zip(events, ("schema", "pause", "hosted"), strict=True):
        assert_event(event, server.name, "inspect", "rejected", stage)
    assert_redacted(events, "pause-private", "hosted-private")


async def test_remote_discovery_read_and_observer_denial():
    remote = FakeRemote()
    events, observations = [], []

    async def observer(server, name, args, result):
        observations.append((server, name, args, result))
        if args["query"] == "deny-private":
            raise RuntimeError("observer-private")

    async def handler(args):
        return ok({})

    try:
        bus = SimpleNamespace(paused=False, remote_result_observer=observer)
        server = local(handler, events.append, bus=bus, remote=(_fake(remote),))
        assert len(server.remotes) == 1
        assert events == []  # Discovery and priming are not model calls.
        invoke = server.remote_tables()["fake"]["lookup"].handler
        accepted = await invoke({"query": "read-private"})
        denied = await invoke({"query": "deny-private"})
        assert not accepted.get("is_error")
        assert denied["is_error"]
        assert remote.calls.count("lookup") == 2
        assert len(observations) == 2
        assert len(events) == 2
        assert_event(events[0], "fake", "lookup", "success", "handler")
        assert_event(events[1], "fake", "lookup", "failure", "observer")
        assert_redacted(events, "read-private", "deny-private", "observer-private")
    finally:
        remote.stop()


async def test_remote_projection_failure_is_recorded_and_delivers_nothing():
    remote = FakeRemote()
    events, observations = [], []

    async def projector(server, name, args, raw):
        if args["query"] == "fail-private":
            raise RuntimeError("projector-private")
        return [{"type": "text", "text": "projected"}]

    async def observer(*values):
        observations.append(values)

    async def handler(args):
        return ok({})

    try:
        bus = SimpleNamespace(
            paused=False, remote_result_projector=projector, remote_result_observer=observer
        )
        server = local(handler, events.append, bus=bus, remote=(_fake(remote),))
        invoke = server.remote_tables()["fake"]["lookup"].handler
        accepted = await invoke({"query": "read-private"})
        failed = await invoke({"query": "fail-private"})
        assert accepted == {"content": [{"type": "text", "text": "projected"}]}
        assert failed["is_error"]
        assert len(observations) == 1
        assert_event(events[0], "fake", "lookup", "success", "handler")
        assert_event(events[1], "fake", "lookup", "failure", "projection")
        assert_redacted(events, "read-private", "fail-private", "projector-private")
    finally:
        remote.stop()


async def test_handler_exception_and_cancellation_are_terminal_and_not_success():
    events = []
    entered = asyncio.Event()

    async def handler(args):
        if args["query"] == "raise":
            raise RuntimeError("exception-private")
        entered.set()
        await asyncio.Event().wait()

    server = local(handler, events.append)
    invoke = server.tool_table()["inspect"].handler
    failure = await invoke({"query": "raise"})
    assert failure["is_error"]
    pending = asyncio.create_task(invoke({"query": "cancel-private"}))
    await asyncio.wait_for(entered.wait(), 5)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(events) == 2
    assert_event(events[0], server.name, "inspect", "exception", "handler")
    assert_event(events[1], server.name, "inspect", "cancelled", "handler")
    assert_redacted(events, "exception-private", "cancel-private")


async def test_timeout_is_recorded_as_incomplete_not_failure_or_success():
    events = []

    async def handler(args):
        raise TimeoutError("timeout-private")

    server = local(handler, events.append)
    result = await server.tool_table()["inspect"].handler({"query": "query-private"})
    assert result["is_error"]
    assert len(events) == 1
    assert_event(events[0], server.name, "inspect", "incomplete", "handler")
    assert field(events[0], "is_error") is True
    assert_redacted(events, "timeout-private", "query-private")


async def test_discovered_remote_sdk_rejection_is_recorded_without_proxying():
    remote = FakeRemote()
    events = []

    async def handler(args):
        return ok({})

    try:
        server = local(handler, events.append, remote=(_fake(remote),))
        assert len(server.remotes) == 1
        before = list(remote.calls)
        secret = "remote-candidate-private-" + "z" * 20000
        async with mcp_client(server.remotes[0].server["instance"]) as client:
            rejected = await client.call_tool("lookup", {"query": [secret]})
            accepted = await client.call_tool("lookup", {"query": "remote-read-private"})
        assert rejected.is_error and not accepted.is_error
        assert remote.calls == before + ["lookup"]
        assert len(events) == 2
        assert_event(events[0], "fake", "lookup", "rejected", "sdk")
        assert_event(events[1], "fake", "lookup", "success", "handler")
        assert_redacted(events, "remote-candidate-private", "remote-read-private")
    finally:
        remote.stop()


async def test_recorder_failure_and_mutation_cannot_change_call():
    seen = []
    arguments = {"query": "argument-private"}

    async def handler(args):
        seen.append(args.copy())
        return ok({"answer": "result-private"})

    def recorder(event):
        arguments_summary = field(event, "arguments")
        result_summary = field(event, "result")
        if isinstance(arguments_summary, dict):
            arguments_summary.clear()
        if isinstance(result_summary, dict):
            result_summary.clear()
        raise RuntimeError("recorder-private")

    server = local(handler, recorder)
    result = await server.tool_table()["inspect"].handler(arguments)
    assert arguments == {"query": "argument-private"}
    assert seen == [arguments]
    assert not result.get("is_error")
    assert json.loads(result["content"][0]["text"]) == {"answer": "result-private"}


async def test_argument_and_result_summaries_are_bounded():
    events = []
    secret = "long-private-material-" + "q" * 200000

    async def handler(args):
        return ok({"answer": secret})

    server = local(handler, events.append)
    result = await server.tool_table()["inspect"].handler({"query": secret})
    assert not result.get("is_error")
    assert len(events) == 1
    assert_event(events[0], server.name, "inspect", "success")
    for summary in (field(events[0], "arguments"), field(events[0], "result")):
        assert set(summary) == {"hmac_sha256", "size"}
        assert summary["hmac_sha256"] is None or len(summary["hmac_sha256"]) == 64
        assert summary["size"] is None or isinstance(summary["size"], int)
    assert_redacted(events, "long-private-material")


async def test_summaries_are_keyed_per_recorder_and_correlate_only_within_run(monkeypatch):
    from annealage_ember import tools as tools_module

    secret = "yes"
    keys = (b"first-recorder-key-" + b"a" * 14, b"second-recorder-key-" + b"b" * 13)
    events_a, events_b = [], []

    async def handler(args):
        return ok({"answer": secret})

    with monkeypatch.context() as patch:
        generated = iter(keys)
        patch.setattr(tools_module.secrets, "token_bytes", lambda size: next(generated))
        server_a = local(handler, events_a.append)
        server_b = local(handler, events_b.append)

    args = {"query": secret}
    result = await server_a.tool_table()["inspect"].handler(args)
    await server_a.tool_table()["inspect"].handler(args)
    await server_b.tool_table()["inspect"].handler(args)

    def public_digest(value):
        encoded = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    assert len(events_a) == 2 and len(events_b) == 1
    for name, value in (("arguments", args), ("result", result)):
        first = field(events_a[0], name)["hmac_sha256"]
        assert first is not None
        assert first == field(events_a[1], name)["hmac_sha256"]
        assert first != field(events_b[0], name)["hmac_sha256"]
        assert first != public_digest(value)
        assert field(events_b[0], name)["hmac_sha256"] != public_digest(value)
    assert_redacted(events_a + events_b, secret, *(key.decode() for key in keys))
    assert all(key.hex() not in repr(events_a + events_b) for key in keys)


async def test_sent_remote_mcp_error_is_incomplete_and_write_is_not_replayed(monkeypatch, tmp_path):
    from mcp import MCPError

    from annealage_ember import remote as remote_module
    from annealage_ember.remote_actions import RemoteActionStore

    remote = FakeRemote()
    events = []

    async def handler(args):
        return ok({})

    try:
        bus = SimpleNamespace(
            paused=False,
            remote_action_store=RemoteActionStore(
                tmp_path / "remote-actions.json", project_id=str(tmp_path), session="sess-1"
            ),
        )
        server = local(handler, events.append, bus=bus, remote=(_fake(remote),))
        original_call = remote_module._call

        async def sent_then_error(session, name, arguments):
            await original_call(session, name, arguments)
            raise MCPError(-32603, "remote-private")

        monkeypatch.setattr(remote_module, "_call", sent_then_error)
        invoke = server.remote_tables()["fake"]["store"].handler
        first = await invoke({"text": "write-private"})
        second = await invoke({"text": "write-private"})

        assert first["is_error"] and second["is_error"]
        assert remote.calls.count("store") == 1
        assert remote.stored == ["write-private"]
        assert len(events) == 2
        assert_event(events[0], "fake", "store", "incomplete", "handler")
        assert field(events[0], "is_error") is True
        assert_event(events[1], "fake", "store", "rejected", "policy")
        assert_redacted(events, "write-private", "remote-private")
    finally:
        remote.stop()


async def test_http_transport_broker_denial_is_recorded_before_handler():
    from conftest import make_test_client
    from test_mcp_bridge import CountingBroker, _mcp_app, _post

    from annealage_ember.session.permissions import Decision

    calls, events = [], []

    async def handler(args):
        calls.append(args)
        return ok({"accepted": True})

    server = local(handler, events.append, grade="write")
    broker = CountingBroker(Decision(allow=False, message="denial-private"))
    client = make_test_client(_mcp_app(server, broker=broker))
    response = await _post(
        client,
        {
            "method": "tools/call",
            "params": {"name": "inspect", "arguments": {"query": "permission-private"}},
        },
    )
    assert response.status_code == 200
    assert json.loads(response.body)["result"]["isError"] is True
    assert len(broker.calls) == 1
    assert calls == []
    assert len(events) == 1
    assert_event(events[0], server.name, "inspect", "rejected", "policy")
    assert_redacted(events, "permission-private", "denial-private")


async def test_sdk_pre_tool_use_config_refusal_is_recorded(tmp_path):
    from annealage_ember.session import workspace_trust
    from annealage_ember.session.sdk import SdkSession

    calls, events = [], []

    async def handler(args):
        calls.append(args)
        return ok({})

    server = local(handler, events.append, grade="write")
    digest = workspace_trust.config_digest(tmp_path)
    session = SdkSession(
        lambda event: None,
        cwd=tmp_path,
        session_id="session",
        trusted_config_digest=digest,
        mcp_servers=server.mcp_servers,
    )
    (tmp_path / ".mcp.json").write_text('{"mcpServers":{"private":{}}}')
    hook = session._build_options().hooks["PreToolUse"][0].hooks[0]
    output = await hook(
        {"tool_name": f"mcp__{server.name}__inspect", "tool_input": {"query": "hook-private"}},
        "tool-use",
        None,
    )
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == []
    assert len(events) == 1
    assert_event(events[0], server.name, "inspect", "rejected", "policy")
    assert_redacted(events, "hook-private")


async def test_sdk_permission_broker_refusal_is_recorded(tmp_path):
    from claude_agent_sdk import PermissionResultDeny
    from test_mcp_bridge import CountingBroker

    from annealage_ember.session.permissions import Decision
    from annealage_ember.session.sdk import SdkSession

    calls, events = [], []

    async def handler(args):
        calls.append(args)
        return ok({})

    server = local(handler, events.append, grade="write")
    broker = CountingBroker(Decision(allow=False, message="broker-private"))
    session = SdkSession(
        lambda event: None,
        cwd=tmp_path,
        session_id="session",
        broker=broker,
        mcp_servers=server.mcp_servers,
    )
    callback = session._build_options().can_use_tool
    result = await callback(
        f"mcp__{server.name}__inspect", {"query": "sdk-permission-private"}, None
    )
    assert isinstance(result, PermissionResultDeny)
    assert len(broker.calls) == 1
    assert calls == []
    assert len(events) == 1
    assert_event(events[0], server.name, "inspect", "rejected", "policy")
    assert_redacted(events, "sdk-permission-private", "broker-private")
