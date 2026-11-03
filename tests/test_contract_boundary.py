"""Registered tool input is checked before a product handler can act."""

import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from claude_agent_sdk import tool
from mcp import types

from annealage_ember import remote
from annealage_ember.tools import Grading, ToolServer, ok

pytestmark = pytest.mark.asyncio


SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"$ref": "#/$defs/item"},
        },
    },
    "required": ["items"],
    "additionalProperties": False,
    "$defs": {
        "item": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["name", "count"],
            "additionalProperties": False,
        },
    },
}


def registered(schema, calls):
    @tool("nested", "Accept nested items.", schema)
    async def nested(args):
        calls.append(args)
        return ok({"accepted": True})

    server = ToolServer(
        [nested],
        grading=Grading(read=("nested",), view=(), write=()),
        bus=SimpleNamespace(paused=False),
        paused_message="paused",
    )
    return server.tools[0].handler


def text(result):
    return "\n".join(block["text"] for block in result["content"] if block["type"] == "text")


@pytest.mark.parametrize(
    "item",
    [
        {"name": "one"},
        {"name": "one", "count": 1, "unexpected": "value"},
        {"name": "one", "count": "1"},
        {"name": "one", "count": True},
        {"name": None, "count": 1},
    ],
)
async def test_invalid_nested_items_never_reach_handler(item):
    calls = []
    handler = registered(SCHEMA, calls)
    result = await handler({"items": [item]})

    assert result.get("is_error") is True
    assert calls == []
    diagnostic = json.loads(text(result))
    assert diagnostic["code"] == "invalid_input"
    assert diagnostic["issues"]
    assert "items" in json.dumps(diagnostic["issues"])
    assert any(issue.get("code") for issue in diagnostic["issues"])


async def test_valid_nested_input_reaches_handler_unchanged():
    calls = []
    handler = registered(SCHEMA, calls)
    payload = {"items": [{"name": "one", "count": 1}, {"name": "two", "count": 0}]}

    result = await handler(payload)

    assert not result.get("is_error", False)
    assert calls == [payload]
    assert json.loads(text(result))["accepted"] is True


async def test_wide_nested_arrays_preserve_values_and_error_locations():
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "integer"}},
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    calls = []
    handler = registered(schema, calls)
    payload = {"items": [[index, index + 1] for index in range(65)]}

    result = await handler(payload)

    assert not result.get("is_error", False)
    assert calls == [payload]
    assert json.loads(text(result))["accepted"] is True

    invalid = {"items": [row.copy() for row in payload["items"]]}
    invalid["items"][64][1] = "not an integer"
    result = await handler(invalid)

    assert result.get("is_error") is True
    assert calls == [payload]
    diagnostic = json.loads(text(result))
    assert diagnostic["code"] == "invalid_input"
    assert any(
        list(map(str, issue["location"])) == ["items", "64", "1"] and issue["code"] == "type"
        for issue in diagnostic["issues"]
    )

    invalid["items"][64][1] = float("inf")
    result = await handler(invalid)

    assert result.get("is_error") is True
    assert calls == [payload]
    diagnostic = json.loads(text(result))
    assert any(
        list(map(str, issue["location"])) == ["items", "64", "1"] and issue["code"] == "number"
        for issue in diagnostic["issues"]
    )


async def test_remote_schema_reference_cannot_fetch(monkeypatch):
    attempts = []

    def no_connect(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("schema resolution attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_connect)
    calls = []
    schema = {
        "type": "object",
        "properties": {"item": {"$ref": "https://example.invalid/schema.json"}},
    }
    with pytest.raises((ValueError, TypeError, RuntimeError)):
        registered(schema, calls)
    assert attempts == []
    assert calls == []


async def test_validation_diagnostic_is_bounded_and_does_not_echo_values():
    calls = []
    handler = registered(SCHEMA, calls)
    secret = "secret-value-" + "x" * 20000
    result = await handler({"items": [{"name": secret, "count": secret} for _ in range(500)]})

    assert result.get("is_error") is True
    assert calls == []
    rendered = text(result)
    assert len(rendered) < 20000
    assert "secret-value-" not in rendered
    diagnostic = json.loads(rendered)
    assert diagnostic["code"] == "invalid_input"
    assert diagnostic["truncated"] > 0


async def test_remote_failure_preserves_safe_structural_diagnostic(monkeypatch):
    secret = "sk-private-token-value"
    url = "https://internal.example/secret"
    upstream = {
        "issues": [
            {
                "location": ["items", 0, "count"],
                "code": "type",
                "message": f"bad value {secret} at {url}",
            },
            {"location": [secret], "code": "required", "message": secret},
        ],
        "extra": secret,
    }

    @asynccontextmanager
    async def session(_server):
        yield object(), None

    async def call_remote(_session, _name, _arguments):
        return SimpleNamespace(
            is_error=True,
            content=[types.TextContent(type="text", text=json.dumps(upstream))],
            structured_content=None,
        )

    monkeypatch.setattr(remote, "_session", session)
    monkeypatch.setattr(remote, "_call", call_remote)
    server = remote.RemoteServer("example", "https://example.invalid", Grading(("nested",), (), ()))
    tool_def = SimpleNamespace(name="nested", description="", input_schema=SCHEMA)
    handler = remote._proxied(server, tool_def).handler

    result = await handler({"items": [{"name": "part", "count": 1}]})

    assert result["is_error"] is True
    rendered = text(result)
    assert secret not in rendered
    assert url not in rendered
    diagnostic = json.loads(rendered)
    assert diagnostic["code"] == "remote_failure"
    assert diagnostic["issues"][0] == {
        "location": ["items", "0", "count"],
        "code": "type",
        "message": "A field has the wrong type",
    }
    assert diagnostic["issues"][1]["location"] == ["<field>"]


async def test_remote_failure_does_not_forward_arbitrary_payload(monkeypatch):
    secret = "bearer-not-for-output"
    upstream = {
        "detail": [
            {"loc": ["items", secret], "type": "int_parsing", "msg": secret, "input": secret}
            for _ in range(100)
        ]
    }

    @asynccontextmanager
    async def session(_server):
        yield object(), None

    async def call_remote(_session, _name, _arguments):
        return SimpleNamespace(
            is_error=True,
            content=[types.TextContent(type="text", text=secret * 100000)],
            structured_content=upstream,
        )

    monkeypatch.setattr(remote, "_session", session)
    monkeypatch.setattr(remote, "_call", call_remote)
    server = remote.RemoteServer("example", "https://example.invalid", Grading(("nested",), (), ()))
    handler = remote._proxied(
        server, SimpleNamespace(name="nested", description="", input_schema=SCHEMA)
    ).handler
    result = await handler({"items": [{"name": "part", "count": 1}]})

    rendered = text(result)
    assert result["is_error"] is True
    assert secret not in rendered
    assert len(rendered) < 5000
    diagnostic = json.loads(rendered)
    assert len(diagnostic["issues"]) == 16
    assert diagnostic["issues"][0] == {
        "location": ["items", "<field>"],
        "code": "int_parsing",
        "message": "Expected an integer",
    }


async def test_remote_failure_unrecognised_text_is_not_exposed():
    result = remote._result(
        "example",
        "nested",
        SimpleNamespace(
            is_error=True,
            content=[
                types.TextContent(
                    type="text", text="failure: bearer secret at https://private.example/path"
                )
            ],
            structured_content=None,
        ),
    )
    assert result["is_error"] is True
    rendered = text(result)
    assert "secret" not in rendered
    assert "private.example" not in rendered
    assert json.loads(rendered)["issues"][0]["code"] == "remote_error"
