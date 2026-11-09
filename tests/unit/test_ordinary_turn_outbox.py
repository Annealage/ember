"""Ordinary turn outbox behavior through the real admission and session path."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from conftest import create_toy_app

from annealage_ember import sessions
from annealage_ember.identity import Human
from annealage_ember.review.notifications import NotificationError
from annealage_ember.review.outbox import OrdinaryTurnOutbox
from annealage_ember.session.base import TurnEnd
from annealage_ember.session.fake import FakeSession

pytestmark = pytest.mark.asyncio


class ClaimsAuth:
    def __init__(self):
        self.checked = []

    def permits(self, human, operation):
        self.checked.append((human, operation))
        return human.hosted_claims is not None and operation in human.hosted_claims.ops


def human(principal="alice", ops=("project.exec",)):
    return Human(
        login=principal + "@example.com",
        name=principal.title(),
        principal_id=principal,
        workspace_id="workspace",
        project_id="project",
        hosted_claims=SimpleNamespace(
            sub=principal,
            wsp="workspace",
            prj="project",
            ops=list(ops),
            job="job",
            rev=1,
            exp=2000000000,
        ),
    )


def blocks(text):
    return [{"type": "text", "text": text}]


@pytest_asyncio.fixture
async def outbox_app(served_dir):
    sid = sessions.create_session(served_dir)
    app = create_toy_app(
        served_dir,
        session_id=sid,
        build_session=lambda on_event, **_: FakeSession(on_event, session_id=sid),
    )
    auth = ClaimsAuth()
    path = served_dir / "ordinary-turns.json"
    outbox = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    try:
        yield app, outbox, auth, path
    finally:
        await app.agent_stop()


def finish_turn(app):
    app.agent_session.emit(TurnEnd(turn=app.agent_bus.turn, stop_reason="end_turn", cost_usd=0.0))


def submitted(app):
    return [turn[0] for turn in app.agent_session.submitted_turns]


async def test_fifo_delivery_waits_for_previous_turn_and_preserves_attribution(outbox_app):
    app, outbox, _, _ = outbox_app
    alice, bob = human(), human("bob")
    assert outbox.enqueue("first", alice, blocks("one")) == {"id": "first", "status": "queued"}
    assert outbox.enqueue("second", bob, blocks("two")) == {"id": "second", "status": "queued"}

    await outbox.deliver()
    assert submitted(app) == [blocks("one")]
    assert outbox.status("first") == {"id": "first", "status": "sent"}
    assert outbox.status("second") == {"id": "second", "status": "queued"}
    await outbox.deliver()
    assert submitted(app) == [blocks("one")]

    finish_turn(app)
    await outbox.deliver()
    assert submitted(app) == [blocks("one"), blocks("two")]
    assert outbox.status("second") == {"id": "second", "status": "sent"}
    events = [
        wire for _, wire in app.agent_event_log.replay(0).events if wire["kind"] == "user_turn"
    ]
    assert [event["by"] for event in events] == [alice.login, bob.login]
    assert [event["blocks"] for event in events] == [blocks("one"), blocks("two")]
    finish_turn(app)
    await outbox.deliver()
    assert submitted(app) == [blocks("one"), blocks("two")]


async def test_enqueue_is_idempotent_snapshots_blocks_and_rejects_id_reuse(outbox_app):
    app, outbox, _, _ = outbox_app
    original = blocks("original")
    assert outbox.status("missing") is None
    assert outbox.enqueue("op", human(), original)["status"] == "queued"
    original[0]["text"] = "mutated"
    assert outbox.enqueue("op", human(), blocks("original"))["status"] == "queued"
    with pytest.raises(NotificationError, match="different turn or actor"):
        outbox.enqueue("op", human(), blocks("different"))
    with pytest.raises(NotificationError, match="different turn or actor"):
        outbox.enqueue("op", human("bob"), blocks("original"))
    await outbox.deliver()
    assert submitted(app) == [blocks("original")]
    assert outbox.enqueue("op", human(), blocks("original")) == {"id": "op", "status": "sent"}
    finish_turn(app)
    await outbox.deliver()
    assert submitted(app) == [blocks("original")]


@pytest.mark.parametrize("blocker", ["paused", "running", "refusal"])
async def test_blocked_admission_keeps_turn_queued_until_unblocked(
    outbox_app, monkeypatch, blocker
):
    app, outbox, _, _ = outbox_app
    previous = []
    if blocker == "running":
        outbox.enqueue("active", human(), blocks("active"))
        await outbox.deliver()
        previous = [blocks("active")]
        assert submitted(app) == previous
        assert outbox.status("active") == {"id": "active", "status": "sent"}
    outbox.enqueue("op", human(), blocks("wait"))
    if blocker == "paused":
        app.agent_bus.set_paused(True)
    elif blocker == "running":
        assert app.agent_holder.status()["turn_running"]
    else:
        monkeypatch.setattr(app.agent_session, "turn_refusal", lambda: "not ready", raising=False)

    await outbox.deliver()
    assert outbox.status("op") == {"id": "op", "status": "queued"}
    assert submitted(app) == previous
    if blocker == "paused":
        app.agent_bus.set_paused(False)
    elif blocker == "running":
        finish_turn(app)
    else:
        monkeypatch.setattr(app.agent_session, "turn_refusal", lambda: None)
    await outbox.deliver()
    assert outbox.status("op") == {"id": "op", "status": "sent"}
    assert submitted(app) == previous + [blocks("wait")]


async def test_authorization_uses_queued_actor_and_retry_refreshes_claims(outbox_app):
    app, outbox, auth, _ = outbox_app
    outbox.enqueue("op", human(ops=()), blocks("protected"))
    await outbox.deliver()
    assert submitted(app) == []
    status = outbox.status("op")
    assert status["status"] == "queued"
    assert "authorisation expired" in status["error"]
    assert auth.checked[-1][0].principal_id == "alice"
    assert auth.checked[-1][1] == "project.exec"

    # A later operation cannot overtake the denied FIFO head.
    outbox.enqueue("later", human("bob"), blocks("later"))
    await outbox.deliver()
    assert submitted(app) == []
    assert outbox.status("later")["status"] == "queued"
    assert outbox.enqueue("op", human(), blocks("protected")) == {"id": "op", "status": "queued"}
    await outbox.deliver()
    assert submitted(app) == [blocks("protected")]
    assert outbox.status("op")["status"] == "sent"
    assert auth.checked[-1][0].principal_id == "alice"
    finish_turn(app)
    await outbox.deliver()
    assert submitted(app) == [blocks("protected"), blocks("later")]
    assert auth.checked[-1][0].principal_id == "bob"


async def test_queued_retry_refreshes_claims_when_receipt_quota_is_full(outbox_app, monkeypatch):
    from annealage_ember.review import outbox as outbox_module

    app, outbox, auth, path = outbox_app
    monkeypatch.setattr(outbox_module, "_MAX_RECEIPTS", 2)
    first = blocks("already sent")
    protected = blocks("protected")
    outbox.enqueue("sent", human(), first)
    await outbox.deliver()
    finish_turn(app)

    outbox.enqueue("pending", human(ops=()), protected)
    await outbox.deliver()
    assert "authorisation expired" in outbox.status("pending")["error"]
    with pytest.raises(NotificationError, match="Turn receipt capacity reached"):
        outbox.enqueue("new", human(), blocks("new"))
    assert outbox.enqueue("pending", human(), protected) == {"id": "pending", "status": "queued"}
    await outbox.deliver()
    assert auth.checked[-1][0].hosted_claims.ops == ["project.exec"]
    assert submitted(app) == [first, protected]
    finish_turn(app)

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.enqueue("pending", human(), protected)["status"] == "sent"
    await restarted.deliver()
    assert submitted(app) == [first, protected]


async def test_restart_recovers_dispatching_as_uncertain_without_replay(outbox_app):
    app, outbox, auth, path = outbox_app
    outbox.enqueue("crashed", human(), blocks("possibly delivered"))
    outbox.enqueue("next", human(), blocks("next"))
    # The admission fence was persisted, but the process died before recording an outcome.
    outbox.ledger.change(lambda rows: rows["crashed"].update(status="dispatching", turn=1))
    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    status = restarted.status("crashed")
    assert status["status"] == "uncertain"
    assert "will not be replayed" in status["error"]
    assert restarted.ledger.read()["crashed"]["status"] == "uncertain"
    assert restarted.enqueue("crashed", human(), blocks("possibly delivered")) == status
    await restarted.deliver()
    assert submitted(app) == [blocks("next")]
    assert restarted.status("next")["status"] == "sent"
    finish_turn(app)
    await restarted.deliver()
    assert submitted(app) == [blocks("next")]


@pytest.mark.parametrize("crash", [False, True])
async def test_legacy_full_ledger_persists_fence_before_submission(outbox_app, monkeypatch, crash):
    from annealage_ember import files

    app, outbox, auth, path = outbox_app
    content = blocks("legacy " + "x" * 10000)
    outbox.enqueue("legacy", human(), content)
    legacy = path.read_bytes()
    assert json.loads(legacy)["version"] == 1
    monkeypatch.setattr(files, "_MAX_FIXED_FILE_BYTES", len(legacy))
    original_submit = app.agent_session.submit_turn
    fence = None

    class SimulatedCrash(BaseException):
        pass

    async def submit_with_fence_check(*args, **kwargs):
        nonlocal fence
        fence = path.read_bytes()
        assert len(fence) <= len(legacy)
        assert outbox.ledger.read()["legacy"]["status"] == "dispatching"
        if crash:
            raise SimulatedCrash()
        await original_submit(*args, **kwargs)

    monkeypatch.setattr(app.agent_session, "submit_turn", submit_with_fence_check)
    if crash:
        with pytest.raises(SimulatedCrash):
            await outbox.deliver()
        # Restore the last durable image at the crash point; exception cleanup
        # in this still-running process is not part of a real process crash.
        assert fence is not None
        path.write_bytes(fence)
    else:
        await outbox.deliver()
        assert submitted(app) == [content]
        finish_turn(app)

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    expected_status = "uncertain" if crash else "sent"
    assert restarted.status("legacy")["status"] == expected_status
    assert restarted.enqueue("legacy", human(), content)["status"] == expected_status
    await restarted.deliver()
    assert submitted(app) == ([] if crash else [content])


async def test_legacy_full_ledger_retries_and_dispatches_multiple_pending_turns(
    outbox_app, monkeypatch
):
    from annealage_ember import files
    from annealage_ember.review import outbox as outbox_module

    app, outbox, auth, path = outbox_app
    expected = [blocks(f"legacy turn {index}") for index in range(12)]
    for index, content in enumerate(expected):
        outbox.enqueue(str(index), human(), content)

    legacy = path.read_bytes()
    assert json.loads(legacy)["version"] == 1
    rows = outbox.ledger.read()
    one_fence_sizes = []
    for key in rows:
        projected = {name: dict(row) for name, row in rows.items()}
        projected[key].update(status="dispatching", turn=outbox_module._MAX_TURN)
        one_fence_sizes.append(len(outbox_module._compact_payload("project", projected)))
    cap = max(len(legacy), *one_fence_sizes)
    all_fences = {
        key: {**row, "status": "dispatching", "turn": outbox_module._MAX_TURN}
        for key, row in rows.items()
    }
    assert len(outbox_module._compact_payload("project", all_fences)) > cap
    monkeypatch.setattr(files, "_MAX_FIXED_FILE_BYTES", cap)

    assert outbox.enqueue("0", human(), expected[0])["status"] == "queued"
    for index in range(len(expected)):
        await outbox.deliver()
        assert submitted(app) == expected[: index + 1]
        finish_turn(app)

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.enqueue("0", human(), expected[0])["status"] == "sent"
    await restarted.deliver()
    assert submitted(app) == expected


async def test_sent_large_turn_is_compacted_and_remains_idempotent_after_restart(outbox_app):
    app, outbox, auth, path = outbox_app
    content = blocks("large diff: " + "x" * (3 * 1024 * 1024))
    outbox.enqueue("large", human(), content)
    await outbox.deliver()
    assert outbox.status("large") == {"id": "large", "status": "sent"}
    assert path.stat().st_size < 1024 * 1024

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.enqueue("large", human(), content) == {"id": "large", "status": "sent"}
    with pytest.raises(NotificationError, match="different turn or actor"):
        restarted.enqueue("large", human(), blocks("different"))
    with pytest.raises(NotificationError, match="different turn or actor"):
        restarted.enqueue("large", human("bob"), content)
    finish_turn(app)
    await restarted.deliver()
    assert submitted(app) == [content]
    restarted.enqueue("next-large", human(), content)
    await restarted.deliver()
    assert restarted.status("next-large") == {"id": "next-large", "status": "sent"}
    assert submitted(app) == [content, content]
    assert path.stat().st_size < 1024 * 1024
    assert restarted.enqueue("large", human(), content)["status"] == "sent"


async def test_legacy_sent_record_compacts_on_next_change(outbox_app):
    app, outbox, auth, path = outbox_app
    content = blocks("legacy diff: " + "x" * (2 * 1024 * 1024))
    outbox.enqueue("legacy", human(), content)
    data = json.loads(path.read_text())
    data["records"]["legacy"]["status"] = "sent"
    path.write_text(json.dumps(data))

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.status("legacy")["status"] == "sent"
    assert restarted.enqueue("legacy", human(), content)["status"] == "sent"
    assert path.stat().st_size < 1024 * 1024
    with pytest.raises(NotificationError, match="different turn or actor"):
        restarted.enqueue("legacy", human(), blocks("other"))
    await restarted.deliver()
    assert submitted(app) == []


async def test_capacity_failure_preserves_queued_and_uncertain_turns(outbox_app):
    app, outbox, auth, path = outbox_app
    uncertain = blocks("uncertain " + "u" * (1024 * 1024))
    pending = blocks("pending " + "p" * (1024 * 1024))
    outbox.enqueue("uncertain", human(), uncertain)
    outbox.ledger.change(lambda rows: rows["uncertain"].update(status="dispatching", turn=1))
    outbox.enqueue("pending", human(), pending)
    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    before = path.read_bytes()
    with pytest.raises(NotificationError):
        restarted.enqueue("too-large", human(), blocks("é" * (2 * 1024 * 1024)))
    assert path.read_bytes() == before
    assert restarted.status("too-large") is None
    assert restarted.status("uncertain")["status"] == "uncertain"
    assert restarted.enqueue("uncertain", human(), uncertain)["status"] == "uncertain"
    assert restarted.enqueue("pending", human(), pending)["status"] == "queued"
    await restarted.deliver()
    assert submitted(app) == [pending]
    assert restarted.status("pending")["status"] == "sent"
    assert restarted.status("uncertain")["status"] == "uncertain"


async def test_near_capacity_admission_never_strands_fifo_at_dispatch(outbox_app, monkeypatch):
    from annealage_ember import files

    app, _, auth, path = outbox_app
    content = blocks("boundary " + "x" * 10000)
    probe_path = path.with_name("boundary-probe.json")
    probe = OrdinaryTurnOutbox(app, path=probe_path, scope="project", auth=auth, recorder=None)
    probe.enqueue("boundary", human(), content)
    queued_size = probe_path.stat().st_size

    accepted = []
    rejected = []
    expected = []
    for index, extra in enumerate((0, 64, 8192)):
        monkeypatch.setattr(files, "_MAX_FIXED_FILE_BYTES", queued_size + extra)
        candidate_path = path.with_name(f"boundary-{index}.json")
        candidate = OrdinaryTurnOutbox(
            app, path=candidate_path, scope="project", auth=auth, recorder=None
        )
        try:
            candidate.enqueue("boundary", human(), content)
        except NotificationError:
            rejected.append(extra)
            assert candidate.status("boundary") is None
            candidate.enqueue("next", human(), blocks("next"))
            expected.append(blocks("next"))
        else:
            accepted.append(extra)
            expected.append(content)
        await candidate.deliver()
        assert submitted(app) == expected
        finish_turn(app)
        restarted = OrdinaryTurnOutbox(
            app, path=candidate_path, scope="project", auth=auth, recorder=None
        )
        await restarted.deliver()
        assert submitted(app) == expected
        if extra in accepted:
            assert restarted.enqueue("boundary", human(), content)["status"] == "sent"
        else:
            assert restarted.status("next")["status"] == "sent"

    assert rejected and accepted


async def test_many_sent_receipts_do_not_exhaust_active_outbox_or_replay(outbox_app, monkeypatch):
    from annealage_ember import files

    app, outbox, auth, path = outbox_app
    monkeypatch.setattr(files, "_MAX_FIXED_FILE_BYTES", 16 * 1024)
    expected = []
    for index in range(160):
        content = blocks(f"turn {index}")
        assert outbox.enqueue(f"receipt-{index}", human(), content)["status"] == "queued"
        await outbox.deliver()
        expected.append(content)
        assert submitted(app) == expected
        finish_turn(app)

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    for index in range(160):
        operation = f"receipt-{index}"
        content = blocks(f"turn {index}")
        assert restarted.status(operation)["status"] == "sent"
        assert restarted.enqueue(operation, human(), content)["status"] == "sent"
        with pytest.raises(NotificationError, match="different turn or actor"):
            restarted.enqueue(operation, human(), blocks("different"))
    await restarted.deliver()
    assert submitted(app) == expected


async def test_receipt_survives_ledger_replacement_failure_without_replay(outbox_app, monkeypatch):
    from annealage_ember import files

    app, outbox, auth, path = outbox_app
    content = blocks("sent before ledger replacement")
    outbox.enqueue("interrupted", human(), content)
    original_replace = files.atomic_replace
    receipt_written = False

    def interrupt_after_receipt(target, *args, **kwargs):
        nonlocal receipt_written
        if target == path and receipt_written:
            raise OSError("lost ledger replacement")
        result = original_replace(target, *args, **kwargs)
        if target.name.startswith(".turn-receipt-"):
            receipt_written = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(files, "atomic_replace", interrupt_after_receipt)
        with pytest.raises(OSError, match="lost ledger replacement"):
            await outbox.deliver()

    assert receipt_written
    assert submitted(app) == [content]
    assert json.loads(path.read_text())["records"]["interrupted"]["status"] == "dispatching"
    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.status("interrupted") == {"id": "interrupted", "status": "sent"}
    assert restarted.enqueue("interrupted", human(), content)["status"] == "sent"
    with pytest.raises(NotificationError, match="different turn or actor"):
        restarted.enqueue("interrupted", human(), blocks("different"))
    finish_turn(app)
    await restarted.deliver()
    assert submitted(app) == [content]


async def test_receipt_slots_are_reserved_before_dispatch_and_survive_restart(
    outbox_app, monkeypatch
):
    from annealage_ember.review import outbox as outbox_module

    app, outbox, auth, path = outbox_app
    monkeypatch.setattr(outbox_module, "_MAX_RECEIPTS", 2)
    first, second = blocks("first"), blocks("second")
    assert outbox.enqueue("slot-1", human(), first)["status"] == "queued"
    assert outbox.enqueue("slot-2", human(), second)["status"] == "queued"
    before = path.read_bytes()
    with pytest.raises(NotificationError, match="Turn receipt capacity reached"):
        outbox.enqueue("slot-3", human(), blocks("third"))
    assert path.read_bytes() == before
    assert outbox.status("slot-3") is None
    assert outbox.enqueue("slot-1", human(), first)["status"] == "queued"
    assert outbox.enqueue("slot-2", human(), second)["status"] == "queued"

    await outbox.deliver()
    assert submitted(app) == [first]
    assert outbox.status("slot-2")["status"] == "queued"
    assert outbox.enqueue("slot-1", human(), first)["status"] == "sent"
    assert outbox.enqueue("slot-2", human(), second)["status"] == "queued"
    with pytest.raises(NotificationError, match="Turn receipt capacity reached"):
        outbox.enqueue("slot-3", human(), blocks("third"))
    finish_turn(app)
    await outbox.deliver()
    assert submitted(app) == [first, second]
    finish_turn(app)

    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.enqueue("slot-1", human(), first)["status"] == "sent"
    assert restarted.enqueue("slot-2", human(), second)["status"] == "sent"
    with pytest.raises(NotificationError, match="different turn or actor"):
        restarted.enqueue("slot-1", human(), blocks("different"))
    with pytest.raises(NotificationError, match="Turn receipt capacity reached"):
        restarted.enqueue("slot-3", human(), blocks("third"))
    await restarted.deliver()
    assert submitted(app) == [first, second]


async def test_receipt_bytes_are_reserved_before_dispatch_and_survive_restart(
    outbox_app, monkeypatch
):
    from annealage_ember.review import outbox as outbox_module

    app, outbox, auth, path = outbox_app
    receipt_size = len(
        json.dumps(
            {
                "version": 1,
                "scope": "project",
                "id": "byte-1",
                "status": "sent",
                "fingerprint": "0" * 64,
            },
            ensure_ascii=False,
        ).encode("utf-8")
    )
    monkeypatch.setattr(outbox_module, "_MAX_RECEIPT_BYTES", receipt_size)
    first = blocks("first")
    assert outbox.enqueue("byte-1", human(), first)["status"] == "queued"
    before = path.read_bytes()
    with pytest.raises(NotificationError, match="Turn receipt capacity reached"):
        outbox.enqueue("byte-2", human(), blocks("second"))
    assert path.read_bytes() == before
    assert outbox.status("byte-2") is None
    assert outbox.enqueue("byte-1", human(), first)["status"] == "queued"

    await outbox.deliver()
    assert submitted(app) == [first]
    finish_turn(app)
    restarted = OrdinaryTurnOutbox(app, path=path, scope="project", auth=auth, recorder=None)
    assert restarted.enqueue("byte-1", human(), first)["status"] == "sent"
    with pytest.raises(NotificationError, match="different turn or actor"):
        restarted.enqueue("byte-1", human(), blocks("different"))
    with pytest.raises(NotificationError, match="Turn receipt capacity reached"):
        restarted.enqueue("byte-2", human(), blocks("second"))
    await restarted.deliver()
    assert submitted(app) == [first]
