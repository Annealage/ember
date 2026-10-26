"""What the agent reads never calls the person it works with "the human".

The agent is addressed as a colleague talking to the person using the
service: "you" to their face, "the product owner" when speaking about them.
This builds the text an agent actually receives (the session's system prompt,
the review tools' descriptions and the results they return, the permission
refusals, the viewer and upload notes) and checks it, rather than pinning any
one sentence.
"""

import asyncio
import json
from types import SimpleNamespace

from toy_product import TOY_PAUSED_MESSAGE, build_toy_tools, toy_review_store

from annealage_ember import launch, sessions, uploads, viewers
from annealage_ember import settings as settings_module
from annealage_ember.product import OWNER_WORDING_RULE
from annealage_ember.review.tools import review_tools
from annealage_ember.session import permissions
from annealage_ember.session.permissions import Decision
from annealage_ember.tools import Grading, ToolServer

FORBIDDEN = "the human"


class _Bus:
    paused = False

    def __init__(self, store, broker=None):
        self.review_store = store
        self.broker = broker

    async def call(self, method, params=None, *, timeout=None):
        return {}


class _Broker:
    def __init__(self, decision):
        self.decision = decision

    async def ask(self, tool_name, input_data, context):
        return self.decision


def _system_prompt(tmp_path):
    session_id = sessions.create_session(tmp_path)
    bus = SimpleNamespace(
        tools=build_toy_tools(SimpleNamespace(paused=False), tmp_path, session_id),
        broker=None,
        url="http://127.0.0.1:8765/",
    )
    session = launch.build_session(
        "claude",
        lambda event: None,
        bus=bus,
        serve_dir=tmp_path,
        session_id=session_id,
        resumed=False,
        settings=settings_module.resolve(tmp_path),
        mcp_host="127.0.0.1",
        mcp_port=8765,
        agent_token="agent",
    )
    return session._build_options().system_prompt


def _review_server(store, broker):
    grading = Grading(("list_comments", "resolve_comment"), ("add_callout",), ("delete_callout",))
    bus = _Bus(store, broker)
    return ToolServer(
        review_tools(store, bus=bus), grading=grading, bus=bus, paused_message=TOY_PAUSED_MESSAGE
    )


async def _results(store, broker):
    handlers = {t.name: t.handler for t in _review_server(store, broker).tools}
    comment = store.add_comment(
        anchor={"card": "front", "x": 10, "y": 10}, text="too thin", author="human"
    ).comment
    texts = [
        await handlers["list_comments"]({"status": "all"}),
        await handlers["add_callout"]({"card": "front", "x": 1, "y": 1, "text": " "}),
        await handlers["delete_callout"]({"id": comment.id}),
        await handlers["resolve_comment"]({"id": comment.id, "note": "thickened"}),
    ]
    return [json.dumps(r) for r in texts]


def _tool_descriptions(store):
    return [t.description + json.dumps(t.input_schema) for t in _review_server(store, None).tools]


def test_nothing_the_agent_reads_calls_the_owner_the_human(tmp_path):
    prompt = _system_prompt(tmp_path)
    assert prompt.startswith(OWNER_WORDING_RULE)

    store = toy_review_store(tmp_path)
    texts = [prompt.replace(OWNER_WORDING_RULE, "")]
    texts += _tool_descriptions(store)
    for decision in (
        None,
        Decision(allow=False),
        Decision(allow=True),
    ):
        scratch_dir = tmp_path / ("scratch-%s" % len(texts))
        scratch_dir.mkdir()
        scratch = toy_review_store(scratch_dir)
        broker = None if decision is None else _Broker(decision)
        texts += asyncio.run(_results(scratch, broker))

    texts += [
        permissions.DEFAULT_DENY_MESSAGE,
        permissions._DENY_TIMEOUT_TEMPLATE % {"timeout": 300, "product": "toy"},
        permissions._DENY_ALL_GONE_TEMPLATE % {"product": "toy"},
        permissions._DENY_SHUTDOWN_TEMPLATE % {"product": "toy"},
        viewers.NO_VIEWER_MESSAGE.format(url="http://127.0.0.1:8765/"),
        viewers._NOTE_OPEN % "toy",
    ]
    action = uploads.UploadAction(
        name="send", label="Send", tool=("remote", "tool"), build_args=lambda upload, who: {}
    )
    upload = uploads.Upload("a.pdf", "a.pdf", 1, "application/pdf", tmp_path / "a.pdf")
    for label in (None, "Ada"):
        texts.append(uploads.note(action, upload, SimpleNamespace(label=label), False, "done"))

    offenders = [t for t in texts if FORBIDDEN in t.lower()]
    assert offenders == []
