"""Context attachments: a ``context`` block on a turn frame, resolved by the
product (``Product.resolve_context``) into what the agent reads, while the
event log keeps the block as sent (``turn_context.py``, ``http/ws.py``).

What these pin: the frame's shape and its bounds; the agent receiving the
product's text and image in the same user turn as the message, through the
real send path (``ws._dispatch``) into both backends Loom runs (the Claude SDK
session over a fake transport, the omp session over a fake RPC client); the
log and the transcript keeping the chip, not the resolution; and a message
whose context cannot be resolved being refused back by its ``client_id``,
never sent without it.
"""

import asyncio
import dataclasses

import pytest
from test_omp_session import FakeRpcClient, _tool_table
from test_turn_frames import RecordingSocket, StubRegistry, _Conn, _logged
from test_turn_images import FakeTransport, _png_bytes
from toy_product import TOY

from annealage_ember import product
from annealage_ember.http import ws as ws_module
from annealage_ember.protocol import MAX_CONTEXT_BLOCKS, PROTOCOL_VERSION, validate_inbound
from annealage_ember.session.base import AGENT_READY
from annealage_ember.session.events import EventLog, render_transcript
from annealage_ember.session.fake import FakeSession
from annealage_ember.session.omp import OmpSession
from annealage_ember.session.permissions import PermissionBroker
from annealage_ember.session.sdk import SdkSession
from annealage_ember.turn_context import ContextRefused, agent_blocks
from annealage_ember.viewers import ViewerBus

DATA = {"surface": "sheet", "sheet": "Drivers", "parts": ["U5"]}
CHIP = {"type": "context", "label": "U5 on Drivers", "data": DATA}


def _resolver(seen=None, *, image=True):
    def resolve(bus, serve_dir, data):
        if seen is not None:
            seen.append((bus, serve_dir, data))
        return product.ResolvedContext(
            text="Sheet Drivers. U5: DRV8825, pin 1 CP1 on net CP1.",
            images=(_png_bytes(b"crop"),) if image else (),
        )

    return resolve


def _bus(serve_dir):
    bus = ViewerBus(None, url="http://127.0.0.1:8765/")
    bus.serve_dir = str(serve_dir)
    return bus


async def _send(session, bus, blocks, *, log, client_id="c-1"):
    sock = RecordingSocket()
    frame = {"v": PROTOCOL_VERSION, "type": "turn", "blocks": blocks, "client_id": client_id}
    await ws_module._dispatch(sock, _Conn(), StubRegistry(), log, "tok", frame, session, bus)
    return sock.sent


# --- the frame ---------------------------------------------------------------


def test_a_context_block_is_a_well_shaped_turn_block():
    ok, frame = validate_inbound(
        {
            "v": PROTOCOL_VERSION,
            "type": "turn",
            "blocks": [{"type": "text", "text": "move it"}, CHIP],
        }
    )
    assert ok, frame


@pytest.mark.parametrize(
    "block, words",
    [
        ({"type": "context", "label": "", "data": {}}, "label"),
        ({"type": "context", "label": "x" * 201, "data": {}}, "label"),
        ({"type": "context", "label": "U5", "data": ["U5"]}, "data must be an object"),
        ({"type": "context", "label": "U5", "data": {"x": "y" * 20000}}, "bytes of JSON"),
        ({"type": "context", "label": "U5"}, "data"),
    ],
)
def test_a_malformed_context_block_is_refused_by_the_protocol(block, words):
    ok, reason = validate_inbound({"v": PROTOCOL_VERSION, "type": "turn", "blocks": [block]})
    assert not ok
    assert words in reason


def test_a_turn_carries_a_bounded_number_of_context_blocks():
    blocks = [dict(CHIP, label="chip %d" % i) for i in range(MAX_CONTEXT_BLOCKS + 1)]
    ok, reason = validate_inbound({"v": PROTOCOL_VERSION, "type": "turn", "blocks": blocks})
    assert not ok and "at most" in reason


# --- the resolution ----------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_resolver_the_message_is_refused_not_sent_without_its_context(tmp_path):
    with pytest.raises(ContextRefused, match="does not take attached context"):
        await agent_blocks([CHIP], bus=_bus(tmp_path), serve_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_a_message_without_context_is_left_alone_and_the_product_is_not_asked(
    tmp_path, swap_product
):
    seen = []
    swap_product(dataclasses.replace(TOY, resolve_context=_resolver(seen)))
    blocks = [{"type": "text", "text": "hello"}]
    assert await agent_blocks(blocks, bus=_bus(tmp_path), serve_dir=str(tmp_path)) is blocks
    assert seen == []


@pytest.mark.asyncio
async def test_context_resolves_to_marked_text_and_an_image_written_under_images(
    tmp_path, swap_product
):
    seen = []
    swap_product(dataclasses.replace(TOY, resolve_context=_resolver(seen)))
    bus = _bus(tmp_path)
    out = await agent_blocks(
        [{"type": "text", "text": "put it left of U4"}, CHIP], bus=bus, serve_dir=str(tmp_path)
    )
    assert seen == [(bus, str(tmp_path), DATA)]
    assert out[0] == {"type": "text", "text": "put it left of U4"}
    assert out[1]["type"] == "text"
    assert out[1]["text"].startswith(
        "[Context the product owner attached to this message: U5 on Drivers]"
    )
    assert "DRV8825" in out[1]["text"]
    assert out[2]["type"] == "image_path"
    assert out[2]["path"].startswith("images/context-")
    assert (tmp_path / out[2]["path"]).read_bytes() == _png_bytes(b"crop")


@pytest.mark.asyncio
async def test_the_product_s_refusal_reaches_the_person_and_is_a_note_only_on_redelivery(
    tmp_path, swap_product
):
    def refuse(bus, serve_dir, data):
        raise product.ContextError("sheet Drivers is not in the current build")

    swap_product(dataclasses.replace(TOY, resolve_context=refuse))
    with pytest.raises(ContextRefused, match="not in the current build"):
        await agent_blocks([CHIP], bus=_bus(tmp_path), serve_dir=str(tmp_path))
    out = await agent_blocks(
        [CHIP], bus=_bus(tmp_path), serve_dir=str(tmp_path), refusal_notes=True
    )
    assert len(out) == 1 and "could not be resolved" in out[0]["text"]
    assert "not in the current build" in out[0]["text"]


@pytest.mark.asyncio
async def test_an_image_that_is_not_a_picture_refuses_the_context(tmp_path, swap_product):
    def resolve(bus, serve_dir, data):
        return product.ResolvedContext(text="x", images=(b"<svg>not a raster</svg>",))

    swap_product(dataclasses.replace(TOY, resolve_context=resolve))
    with pytest.raises(ContextRefused, match="not a PNG"):
        await agent_blocks([CHIP], bus=_bus(tmp_path), serve_dir=str(tmp_path))


# --- the send path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_log_keeps_the_chip_and_the_agent_gets_the_resolution(tmp_path, swap_product):
    swap_product(dataclasses.replace(TOY, resolve_context=_resolver(image=False)))
    log = EventLog()
    session = FakeSession(lambda event: None)
    blocks = [{"type": "text", "text": "rotate it"}, CHIP]
    sent = await _send(session, _bus(tmp_path), blocks, log=log)
    await asyncio.sleep(0)

    assert sent == []
    [logged] = _logged(log, "user_turn")
    assert logged["blocks"] == blocks
    [(agent, _viewer)] = session.submitted_turns
    assert [b["type"] for b in agent] == ["text", "text"]
    assert "DRV8825" in agent[1]["text"]
    # The transcript names the chip, as the page shows it.
    records = [{"seq": seq, "event": wire} for seq, wire in log.replay(0).events]
    assert "[context: U5 on Drivers]" in render_transcript(records)


@pytest.mark.asyncio
async def test_an_unresolvable_context_refuses_the_message_by_its_client_id(tmp_path, swap_product):
    def refuse(bus, serve_dir, data):
        raise product.ContextError("the sketch could not be read")

    swap_product(dataclasses.replace(TOY, resolve_context=refuse))
    log = EventLog()
    session = FakeSession(lambda event: None)
    bus = _bus(tmp_path)
    sent = await _send(
        session, bus, [{"type": "text", "text": "hi"}, CHIP], log=log, client_id="c-9"
    )
    assert [(f["type"], f.get("client_id")) for f in sent] == [("refused", "c-9")]
    assert "the sketch could not be read" in sent[0]["reason"]
    assert _logged(log, "user_turn") == []
    assert session.submitted_turns == []
    assert bus.turn == 0


@pytest.mark.asyncio
async def test_the_claude_backend_receives_the_context_in_the_same_user_turn(
    tmp_path, swap_product
):
    swap_product(dataclasses.replace(TOY, resolve_context=_resolver()))
    transport = FakeTransport()
    session = SdkSession(lambda e: None, cwd=str(tmp_path), session_id="s-1", transport=transport)
    await session.start()
    try:
        assert session.agent_status() == AGENT_READY
        await _send(
            session, _bus(tmp_path), [{"type": "text", "text": "rotate it"}, CHIP], log=EventLog()
        )
        users = [o for o in transport.written if o.get("type") == "user"]
        assert len(users) == 1
        content = users[0]["message"]["content"]
        kinds = [b["type"] for b in content]
        assert "image" in kinds
        text = "\n".join(b["text"] for b in content if b["type"] == "text")
        assert "rotate it" in text
        assert "[Context the product owner attached to this message: U5 on Drivers]" in text
        assert "DRV8825" in text
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_the_omp_backend_receives_the_context_in_the_same_prompt(tmp_path, swap_product):
    swap_product(dataclasses.replace(TOY, resolve_context=_resolver()))
    holder = {}

    def factory(**kwargs):
        holder["fake"] = FakeRpcClient(**kwargs)
        return holder["fake"]

    session = OmpSession(
        lambda e: None,
        cwd=str(tmp_path),
        session_id="s-2",
        broker=PermissionBroker(lambda e: None, timeout=2.0, no_viewer_grace=0.05),
        tool_table=_tool_table(),
        client_factory=factory,
        base_url="http://127.0.0.1:11434/v1",
    )
    await session.start()
    try:
        await _send(
            session, _bus(tmp_path), [{"type": "text", "text": "rotate it"}, CHIP], log=EventLog()
        )
        for _ in range(50):
            if holder["fake"].prompt_calls:
                break
            await asyncio.sleep(0.01)
        [call] = holder["fake"].prompt_calls
        assert "rotate it" in call.message
        assert "U5 on Drivers" in call.message and "DRV8825" in call.message
        assert len(call.images) == 1 and call.images[0]["mimeType"] == "image/png"
    finally:
        await session.close()
