"""Correction guidance must not disclose or misrepresent schema content."""

import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import tool
from mcp import types

from annealage_ember import remote
from annealage_ember.tools import Grading, ToolServer, ok

pytestmark = pytest.mark.asyncio


def _text(result):
    return "\n".join(block["text"] for block in result["content"] if block["type"] == "text")


def _local_handler(schema, calls):
    @tool("inspect", "Inspect an item.", schema)
    async def inspect(args):
        calls.append(args)
        return ok({"accepted": True})

    server = ToolServer(
        [inspect],
        grading=Grading(read=("inspect",), view=(), write=()),
        bus=SimpleNamespace(paused=False),
        paused_message="paused",
    )
    return server.tools[0].handler


def _remote_handler(monkeypatch, schema, calls):
    definition = types.Tool(name="inspect", description="Inspect an item.", input_schema=schema)
    monkeypatch.setattr(remote, "_discover", lambda servers: [("", [definition])])

    async def called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid input reached the remote")

    monkeypatch.setattr(remote, "call", called)
    declaration = remote.RemoteServer(
        "example",
        "https://example.invalid",
        Grading(read=("inspect",), view=(), write=()),
    )
    server = ToolServer(
        [],
        grading=Grading(read=(), view=(), write=()),
        bus=SimpleNamespace(paused=False),
        paused_message="paused",
        remote=(declaration,),
    )
    return server.remotes[0].tools[0].handler


async def test_discovered_schema_descriptions_and_enum_do_not_leak(monkeypatch):
    secret = "arbitrary-private-material-9F43c8"
    schema = {
        "type": "object",
        "description": "private schema description " + secret,
        "properties": {
            "mode": {
                "type": "string",
                "description": "private field description " + secret,
                "enum": ["safe", secret],
            },
        },
        "required": ["mode"],
        "additionalProperties": False,
    }
    calls = []
    handler = _remote_handler(monkeypatch, schema, calls)
    for arguments in ({}, {"mode": "invalid"}):
        result = await handler(arguments)
        assert result.get("is_error") is True
        rendered = _text(result)
        assert secret not in rendered
        diagnostic = json.loads(rendered)
        assert diagnostic["code"] == "invalid_input"
        assert diagnostic["issues"]
    assert calls == []


async def test_local_schema_still_offers_useful_correction():
    calls = []
    handler = _local_handler(
        {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["overview", "detail"],
                    "description": "Choose the report mode.",
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        calls,
    )

    missing = await handler({})
    assert missing.get("is_error") is True
    assert "mode" in _text(missing)
    assert "Choose the report mode" in _text(missing)
    invalid = await handler({"mode": "unknown"})
    assert invalid.get("is_error") is True
    assert "overview" in _text(invalid)
    assert "detail" in _text(invalid)
    assert calls == []


async def test_long_enum_value_is_not_offered_as_a_truncated_choice():
    long_choice = "oversized_choice_" + "x" * 400
    calls = []
    handler = _local_handler(
        {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["overview", long_choice]}},
            "required": ["mode"],
        },
        calls,
    )
    result = await handler({"mode": "invalid"})
    assert result.get("is_error") is True
    rendered = _text(result)
    assert long_choice[:80] not in rendered
    assert calls == []


@pytest.mark.parametrize("arguments", [{}, {"unexpected": 1}])
async def test_long_field_name_is_not_offered_as_a_truncated_field(arguments):
    long_field = "oversized_field_" + "x" * 400
    calls = []
    handler = _local_handler(
        {
            "type": "object",
            "properties": {long_field: {"type": "string"}},
            "required": [long_field],
            "additionalProperties": False,
        },
        calls,
    )
    result = await handler(arguments)
    assert result.get("is_error") is True
    assert long_field[:80] not in _text(result)
    assert calls == []


@pytest.mark.parametrize("kind", ["enum", "required", "declared"])
async def test_aggregate_guidance_limit_never_cuts_a_choice_or_field(kind):
    names = [f"candidate_{index:02d}_" + "x" * 28 for index in range(16)]
    if kind == "enum":
        schema = {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": names}},
        }
        arguments = {"mode": "invalid"}
    else:
        schema = {
            "type": "object",
            "properties": {name: {"type": "string"} for name in names},
            "additionalProperties": False,
        }
        if kind == "required":
            schema["required"] = names
            arguments = {}
        else:
            arguments = {"unexpected": 1}
    calls = []
    result = await _local_handler(schema, calls)(arguments)
    assert result.get("is_error") is True
    rendered = _text(result)
    for name in names:
        assert name[:13] not in rendered or name in rendered
    assert calls == []
