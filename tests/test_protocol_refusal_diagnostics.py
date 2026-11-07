"""Protocol refusals retain only bounded, schema-known structural diagnostics."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import MCPError

from annealage_ember import remote
from annealage_ember.tools import Grading


@pytest.mark.asyncio
async def test_invalid_params_refusal_sanitizes_and_bounds_remote_diagnostics(monkeypatch):
    secret = "private-bearer-token-should-not-escape"
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                },
            },
        },
    }
    issue = {
        "location": ["items", 0, "count"],
        "code": "int_parsing",
        "message": secret,
        "input": secret,
        "context": {"authorization": secret},
    }
    data = {
        "issues": [issue, {"location": [secret], "code": "required", "message": secret}]
        + [issue] * 100,
        "token": secret,
        "url": "https://private.example/" + secret,
    }
    calls = []

    @asynccontextmanager
    async def session(_server):
        yield object(), None

    async def call_remote(_session, name, arguments):
        calls.append((name, arguments))
        raise MCPError(code=-32602, message=secret, data=data)

    monkeypatch.setattr(remote, "_session", session)
    monkeypatch.setattr(remote, "_call", call_remote)
    server = remote.RemoteServer("example", "https://example.invalid", Grading(("nested",), (), ()))
    handler = remote._proxied(
        server, SimpleNamespace(name="nested", description="", input_schema=schema)
    ).handler
    arguments = {"items": [{"count": 1}]}

    result = await handler(arguments)

    assert calls == [("nested", arguments)]
    assert result["is_error"] is True
    rendered = json.dumps(result)
    assert secret not in rendered
    assert "private.example" not in rendered
    assert len(rendered) < 5000
    text = "\n".join(block["text"] for block in result["content"] if block["type"] == "text")
    diagnostic = json.loads(text)
    assert diagnostic["code"] == "remote_refusal"
    assert diagnostic["truncated"] > 0
    assert len(diagnostic["issues"]) <= 16
    assert any(issue["code"] == "-32602" for issue in diagnostic["issues"])
    assert any(
        issue["location"] == ["items", "0", "count"] and issue["code"] == "int_parsing"
        for issue in diagnostic["issues"]
    )
    assert any(
        issue["location"] == ["<field>"] and issue["code"] == "required"
        for issue in diagnostic["issues"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("grading", (Grading(("batch",), (), ()), Grading((), (), ())))
async def test_a_connection_ended_before_a_response_says_so_and_what_to_do(monkeypatch, grading):
    @asynccontextmanager
    async def session(_server):
        yield object(), None

    async def call_remote(_session, name, arguments):
        raise MCPError(code=-32000, message="SSE stream ended without a response")

    monkeypatch.setattr(remote, "_session", session)
    monkeypatch.setattr(remote, "_call", call_remote)
    server = remote.RemoteServer("datum", "https://example.invalid", grading)
    result = await remote.call(
        server,
        "batch",
        {},
        read_only=True,
        allowed_reads=("batch",),
    )
    document = json.loads(result["content"][0]["text"])
    assert document["code"] == "remote_refusal"
    assert "dropped the connection" in document["message"]
    assert "smaller batch" in document["message"]
    assert "had no effect" in document["message"]
    assert document["issues"][0]["code"] == "-32000"
    assert "SSE stream ended without a response" in document["issues"][0]["message"]
